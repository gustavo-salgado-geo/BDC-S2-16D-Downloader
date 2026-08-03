# Importação de Bibliotecas

import os # permite interagir com o sistema operacional.
import re # procurar, validar e manipular padrões em textos
import numpy as np # para trabalhar com os array na normalização de 8 bits 
import requests # usada para fazer requisições HTTP

import pystac_client

# PyQt > para toda a interface gráfica
from qgis.PyQt import QtWidgets
from qgis.core import QgsRasterLayer, QgsVectorLayer, QgsProject
from PyQt5.QtWidgets import QApplication, QTabWidget, QAction
from PyQt5.QtCore import Qt

from osgeo import gdal, ogr, osr, gdal_array
from io import BytesIO

from datetime import datetime, timedelta

from scipy import ndimage

from qgis.PyQt.QtGui import QIcon
#-----------------------------FIM Importação de Bibliotecas -----------------------------------------------------------------------------------------------------------

# ──────────────────────────────────────────────────────────────────────────────
# VETORIZAÇÃO DE NUVENS (SCL)
# ──────────────────────────────────────────────────────────────────────────────
def fill_holes_in_mask(mask):
    # Preenche buracos internos na máscara binária
    return ndimage.binary_fill_holes(mask).astype(np.uint8)

def vectorize_cloud_mask(cloud_path, output_gpkg):
    try:
        cloud_ds = gdal.Open(cloud_path)
        if not cloud_ds:
            return None
        
        band = cloud_ds.GetRasterBand(1)
        cloud_data = band.ReadAsArray()
        projection = cloud_ds.GetProjection()
        geotransform = cloud_ds.GetGeoTransform()
        
        srs = osr.SpatialReference()
        srs.ImportFromWkt(projection)

        drv_gpkg = ogr.GetDriverByName('GPKG')
        if os.path.exists(output_gpkg):
            drv_gpkg.DeleteDataSource(output_gpkg)
        
        out_ds = drv_gpkg.CreateDataSource(output_gpkg)
        out_layer = out_ds.CreateLayer('cloud_mask', srs=srs, geom_type=ogr.wkbPolygon)
        
        out_layer.CreateField(ogr.FieldDefn('is_cloud', ogr.OFTInteger))
        layer_defn = out_layer.GetLayerDefn()

        cloud_values = [3, 8, 9, 10]
        
        geom_collection = ogr.Geometry(ogr.wkbMultiPolygon)

        for val in cloud_values:
            mask = (cloud_data == val).astype(np.uint8)
            if not np.any(mask):
                continue
            
            filled_mask = fill_holes_in_mask(mask)

            mem_drv = gdal.GetDriverByName('MEM')
            tmp_ds = mem_drv.Create('', cloud_ds.RasterXSize, cloud_ds.RasterYSize, 1, gdal.GDT_Byte)
            tmp_ds.SetProjection(projection)
            tmp_ds.SetGeoTransform(geotransform)
            tmp_band = tmp_ds.GetRasterBand(1)
            tmp_band.WriteArray(filled_mask)
            tmp_band.SetNoDataValue(0)

            mem_ogr_drv = ogr.GetDriverByName('Memory')
            temp_mem_ds = mem_ogr_drv.CreateDataSource('mem_ds')
            temp_mem_layer = temp_mem_ds.CreateLayer('temp', srs=srs, geom_type=ogr.wkbPolygon)
            
            gdal.Polygonize(tmp_band, tmp_band, temp_mem_layer, -1, [], callback=None)

            for feature in temp_mem_layer:
                geom = feature.GetGeometryRef()
                if geom is None or geom.IsEmpty():
                    continue

                if not geom.IsValid():
                    geom = geom.Buffer(0)
                    if geom is None or geom.IsEmpty():
                        continue
                
                if geom.GetGeometryType() == ogr.wkbMultiPolygon:
                    for i in range(geom.GetGeometryCount()):
                        geom_collection.AddGeometry(geom.GetGeometryRef(i))
                else:
                    geom_collection.AddGeometry(geom)
            
            temp_mem_ds = None
            tmp_ds = None

        if not geom_collection.IsEmpty():
            dissolved_geom = geom_collection.UnionCascaded()
            
            if not dissolved_geom.IsValid():
                dissolved_geom = dissolved_geom.Buffer(0)
            
            out_layer.StartTransaction()
            min_area_sqm = 10000
            
            if dissolved_geom.GetGeometryType() == ogr.wkbMultiPolygon:
                for i in range(dissolved_geom.GetGeometryCount()):
                    sub_geom = dissolved_geom.GetGeometryRef(i)
                    if sub_geom is None or sub_geom.IsEmpty():
                        continue
                    if sub_geom.GetArea() < min_area_sqm:
                        continue
                    
                    new_feat = ogr.Feature(layer_defn)
                    new_feat.SetField('is_cloud', 1)
                    new_feat.SetGeometry(sub_geom)
                    out_layer.CreateFeature(new_feat)
            else:
                if dissolved_geom.GetArea() >= min_area_sqm:
                    new_feat = ogr.Feature(layer_defn)
                    new_feat.SetField('is_cloud', 1)
                    new_feat.SetGeometry(dissolved_geom)
                    out_layer.CreateFeature(new_feat)
                
            out_layer.CommitTransaction()

        out_ds.FlushCache()
        out_ds = None
        cloud_ds = None
        return output_gpkg

    except Exception as e:
        return None
# ──────────────────────────────────────────────────────────────────────────────

class BDCDialog(QtWidgets.QDialog):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("BDC Downloader - Sentinel-2/MSI L2A | LCF 16 days") #Titulo da Interface
        self.setMinimumWidth(600) # setar tamanho minimo

        tabs = QtWidgets.QTabWidget()# divide em 2 abas

# === ABA 1: Tiles, Data, Diretorio Saida, Opções de Criação ===
        main_widget = QtWidgets.QWidget() # para os botões campos
        main_layout = QtWidgets.QVBoxLayout() # para os textos titulos dos campos
        

        stac_label = QtWidgets.QLabel("<b>STAC API:</b> https://data.inpe.br/bdc/stac/v1/")
        stac_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        stac_label.setStyleSheet("color: #555555;")
        main_layout.addWidget(stac_label)
        
        
        self.tile_input = QtWidgets.QLineEdit() #campo de digitação1
        self.tile_input.setPlaceholderText("Ex: 003016,032010") # coloca este texto como exemplo dentro do campo de digitação1
        main_layout.addWidget(QtWidgets.QLabel("Entre com a Lista de Tiles BDC separados por vírgulas ou espaços:")) # titulo que fica acima do campo de digitação1
        main_layout.addWidget(self.tile_input) # Inseri titulo acima do campo de digitação1
       
        self.date_input = QtWidgets.QLineEdit() # campo de digitação2
        self.date_input.setPlaceholderText("Ex: DD/MM/AAAA") # Marca d'água no formato BR
        main_layout.addWidget(QtWidgets.QLabel("Informe a Data da Coleção (Formato: DD/MM/AAAA):")) # Título atualizado
        main_layout.addWidget(self.date_input) # Insere o campo de digitação2

        self.folder_input = QtWidgets.QLineEdit()#campo de digitação3 vem vazio
        folder_layout = QtWidgets.QHBoxLayout()
        folder_button = QtWidgets.QPushButton("Escolher pasta") # Botão ao lado do campo de digitação3
        folder_button.clicked.connect(self.select_folder) # abrir janela para seleção de diretorio qdo clicar
        folder_layout.addWidget(self.folder_input) # adiciona campo na nova janela que abre
        folder_layout.addWidget(folder_button)
        main_layout.addWidget(QtWidgets.QLabel("Pasta de destino:"))# titulo que fica acima do campo de digitação3
        main_layout.addLayout(folder_layout)#campo de digitação3 recebe valor

        # Opção para escolher entre dado original (bruto) ou normalizado para 8 bits
        self.normalize_checkbox = QtWidgets.QCheckBox("Normalizar RGB para 8 bits (padrão: manter dado original)")
        self.normalize_checkbox.setChecked(False)
        main_layout.addWidget(self.normalize_checkbox)
        
        #Botão de Execução 1 
        self.download_button_alt = QtWidgets.QPushButton("Opção 1 >>>>>  Criar VRT da Composição RGB (R11_G08_B04) - Para Visualização Rápida")#define texto do botão
        self.download_button_alt.clicked.connect(self.process_rgb_stac_vrt)
        main_layout.addWidget(self.download_button_alt)#adiciona  botão

        #Botão de Execução 2
        self.download_button = QtWidgets.QPushButton("Opção 2 >>>>>  Download Composição RGB em 8bits (R11_G08_B04) + Banda PROVENANCE") #define texto do botão
        self.download_button.clicked.connect(self.executar_opcao_2_completa)
        main_layout.addWidget(self.download_button) #adiciona  botão
        
        # CAMPO para Seção de seleção de bandas
        self.band_checkboxes = {}
        bands = ["B01", "B02", "B03", "B04", "B05", "B06", "B07", "B08", "B8A", "B09", "B11", "B12", "NDVI", "EVI", "NBR", "SCL"] # opçoes de escolha dentro do campo
        band_group = QtWidgets.QGroupBox("Selecionar bandas para download individual, caso deseje utilizar a opção 3:")#define texto que fica acima do campo
        band_layout = QtWidgets.QGridLayout() # define campo como um Grid
        for i, band in enumerate(bands):
            checkbox = QtWidgets.QCheckBox(band)
            self.band_checkboxes[band] = checkbox
            band_layout.addWidget(checkbox, i // 4, i % 4)
        band_group.setLayout(band_layout)
        main_layout.addWidget(band_group) # adiciona o grid 4x4 no campo de seleção de bandas
        
        #Botão de Execução 3
        self.download_selected_button = QtWidgets.QPushButton("Opção 3 >>>>>     Download das bandas selecionadas acima")#define texto do botão
        self.download_selected_button.clicked.connect(self.download_selected_bands)
        main_layout.addWidget(self.download_selected_button)
        
       # Botão de Execução 4
        self.download_cloud_button = QtWidgets.QPushButton("Opção 4 >>>>>  Gerar Máscara Vetorial de Nuvens")
        self.download_cloud_button.clicked.connect(self.executar_opcao_4_nuvens)
        main_layout.addWidget(self.download_cloud_button)

        # CAMPO para EXIBIR os LOGs de Processamento
        self.tiles_processed_output = QtWidgets.QTextEdit() # cria campo para receber  textos dos logs
        self.tiles_processed_output.setReadOnly(True)
        main_layout.addWidget(QtWidgets.QLabel("Logs de Processamento:"))#define texto do titulo do campo
        main_layout.addWidget(self.tiles_processed_output) # adiciona campo na Aba1

        
        main_widget.setLayout(main_layout)
        tabs.addTab(main_widget, "Download e Geração de Dados")  # Cria e Nomeia a ABA1 da interface

# === ABA 2: Tile, textos explicativos e Período desejado===
        data_widget = QtWidgets.QWidget()
        data_layout = QtWidgets.QVBoxLayout()

        self.tile_check_input = QtWidgets.QLineEdit()#campo de digitação4
        self.tile_check_input.setPlaceholderText("Ex: 027022") # coloca este texto como exemplo dentro do campo de digitação4
        data_layout.addWidget(QtWidgets.QLabel("Informe um Tile BDC para realizar a busca (formato BBBPPP):"))# titulo que fica acima do campo de digitação1
        data_layout.addWidget(self.tile_check_input)# Inseri titulo acima do campo de digitação4

        data_layout.addWidget(QtWidgets.QLabel("")) # Linha em branco para ganhar um espaço abaixo do campo de digitação4
        data_layout.addWidget(QtWidgets.QLabel("Esta coleção possui dados desde 01/01/2017 com um intervalo de 16 dias entre cada mosaico."))#Texto Informativo1

        data_layout.addWidget(QtWidgets.QLabel(""))# Linha em branco para ganhar um espaço abaixo dos textos informativos
        
        date_range_layout = QtWidgets.QHBoxLayout()#campo seleção de DATA INICIAL
        self.date_start = QtWidgets.QDateEdit()
        self.date_start.setCalendarPopup(True) #Exibição de Calendário
        
        self.date_start.setDisplayFormat("dd/MM/yyyy")
        self.date_start.setDate(datetime.today().replace(month=1, day=1))# Setar para primeiro dia do ano atual

        self.date_end = QtWidgets.QDateEdit()#campo seleção de DATA FINAL
        self.date_end.setCalendarPopup(True)#Exibição de Calendário
        
        self.date_end.setDisplayFormat("dd/MM/yyyy")
        self.date_end.setDate(datetime.today())# Setar para dia atual
        
        #ADICIONA OS CAMPOS DE DATAS FORMATADOS ACIMA
        date_range_layout.addWidget(QtWidgets.QLabel("Data Inicial:"))
        date_range_layout.addWidget(self.date_start)
        date_range_layout.addWidget(QtWidgets.QLabel("Data Final:"))
        date_range_layout.addWidget(self.date_end)
        data_layout.addLayout(date_range_layout)

        
        check_button = QtWidgets.QPushButton("🔍 Buscar Datas no Servidor")# Nome Botão de EXECUÇÃO 4
        check_button.clicked.connect(self.buscar_datas_validas)
        data_layout.addWidget(check_button) # adiciona Botão de EXECUÇÃO 4

        # CAMPO para EXIBIR os Resultados da Busca
        self.output_datas_validas = QtWidgets.QTextEdit()
        self.output_datas_validas.setReadOnly(True)
        data_layout.addWidget(QtWidgets.QLabel("Datas válidas encontradas:"))# Nome do campo de Resultados
        data_layout.addWidget(self.output_datas_validas)

        data_widget.setLayout(data_layout)
        tabs.addTab(data_widget, "Pesquisa de Datas da Coleção") # Cria e Nomeia a ABA2 da interface

        # GERAÇÃO DO Layout principal
        main_layout = QtWidgets.QVBoxLayout()
        main_layout.addWidget(tabs)
        self.setLayout(main_layout)
#-------------------------------FIM GERAÇÃO DA INTERFACE------------------------------------        
        
        
        
#-------------------------------PROCESSOS DA ABA 1------------------------------------
    
    #Função para receber o diretorio de saida
    def select_folder(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Selecione a pasta de destino")
        if folder:
            self.folder_input.setText(folder)
  
    def executar_opcao_2_completa(self):
        # 1. Executa o seu processo original do RGB 8-bits
        self.process_rgb(["B11", "B08", "B04"])
        
        # 2. Avisa no Log que vai começar a baixar a PROVENANCE
        self.tiles_processed_output.append("\n--- Iniciando download da banda PROVENANCE ---")
        QtWidgets.QApplication.processEvents()
        
        # 3. Pega as variáveis da interface
        tiles_text = self.tile_input.text()
        raw_date = self.date_input.text()
        output_folder = self.folder_input.text()

        if not tiles_text or not raw_date or not output_folder:
            return # Se faltar algo, a process_rgb já deu o aviso, então só encerramos aqui.

        # Tratamento da data (DD/MM/YYYY para YYYYMMDD) para evitar erros
        if "/" in raw_date:
            try:
                dia, mes, ano = raw_date.split("/")
                data = f"{ano}{mes}{dia}"
            except ValueError:
                return 
        else:
            data = raw_date

        tiles = [t.strip() for t in re.split(r'[,\s]+', tiles_text) if t.strip()]

        # 4. Conecta no STAC para baixar a PROVENANCE diretamente
        try:
            catalog = pystac_client.Client.open("https://data.inpe.br/bdc/stac/v1/")
        except Exception as e:
            self.tiles_processed_output.append(f"Erro ao acessar STAC: {e}")
            return

        for tile in tiles:
            try:
                search = catalog.search(
                    collections=["S2-16D-2"],
                    query={"bdc:tiles": {"in": [tile]}},
                    datetime=f"{data[0:4]}-{data[4:6]}-{data[6:8]}T00:00:00Z/{data[0:4]}-{data[4:6]}-{data[6:8]}T23:59:59Z"
                )
                items = list(search.items())

                if not items:
                    continue # Se não achou item, o process_rgb já avisou

                item = items[0]
                band = "PROVENANCE"
                
                if band in item.assets:
                    url = item.assets[band].href
                    self.tiles_processed_output.append(f"Baixando {band} para o tile {tile}...")
                    QtWidgets.QApplication.processEvents()

                    # Faz o download real do arquivo
                    response = requests.get(url)
                    if response.status_code == 200:
                        file_name = f"{item.id}_{band}.tif"
                        file_path = os.path.join(output_folder, file_name)
                        
                        with open(file_path, "wb") as f:
                            f.write(response.content)
                        self.tiles_processed_output.append(f"✅ Salvo: {file_name}")
                    else:
                        self.tiles_processed_output.append(f"❌ Erro ao baixar {band}. Servidor retornou: {response.status_code}")
                else:
                    self.tiles_processed_output.append(f"⚠️ Banda {band} não disponível para o tile {tile}.")
                
                QtWidgets.QApplication.processEvents()
            
            except Exception as e:
                self.tiles_processed_output.append(f"❌ Erro na PROVENANCE do tile {tile}: {str(e)}")
                
        self.tiles_processed_output.append(">>> Processo Opção 2 Concluído 100%! <<<")
    
    def executar_opcao_4_nuvens(self):
        self.tiles_processed_output.append("\n--- Iniciando Opção 4: Máscara Vetorial de Nuvens ---")
        QtWidgets.QApplication.processEvents()
        
        tiles_text = self.tile_input.text()
        raw_date = self.date_input.text()
        output_folder = self.folder_input.text()

        if not tiles_text or not raw_date or not output_folder:
            self.tiles_processed_output.append("⚠️ Preencha Tiles, Data e Pasta de destino para prosseguir.")
            return

        # Formatação de data
        if "/" in raw_date:
            try:
                dia, mes, ano = raw_date.split("/")
                data = f"{ano}{mes}{dia}"
            except ValueError:
                self.tiles_processed_output.append("⚠️ Formato de data inválido.")
                return 
        else:
            data = raw_date

        tiles = [t.strip() for t in re.split(r'[,\s]+', tiles_text) if t.strip()]

        try:
            catalog = pystac_client.Client.open("https://data.inpe.br/bdc/stac/v1/")
        except Exception as e:
            self.tiles_processed_output.append(f"❌ Erro ao acessar STAC: {e}")
            return

        for tile in tiles:
            try:
                search = catalog.search(
                    collections=["S2-16D-2"],
                    query={"bdc:tiles": {"in": [tile]}},
                    datetime=f"{data[0:4]}-{data[4:6]}-{data[6:8]}T00:00:00Z/{data[0:4]}-{data[4:6]}-{data[6:8]}T23:59:59Z"
                )
                items = list(search.items())

                if not items:
                    self.tiles_processed_output.append(f"⚠️ Nenhuma imagem encontrada para o tile {tile}.")
                    continue

                item = items[0]
                band = "SCL"
                
                if band in item.assets:
                    url = item.assets[band].href
                    self.tiles_processed_output.append(f"Baixando SCL temporária para o tile {tile}...")
                    QtWidgets.QApplication.processEvents()

                    # Faz o download da banda SCL e salva de forma temporária
                    response = requests.get(url)
                    if response.status_code == 200:
                        temp_scl_path = os.path.join(output_folder, f"temp_{item.id}_{band}.tif")
                        with open(temp_scl_path, "wb") as f:
                            f.write(response.content)
                        
                        self.tiles_processed_output.append("Vetorizando nuvens...")
                        QtWidgets.QApplication.processEvents()
                        
                        # Processo de vetorização (Chama as funções de fora da classe)
                        cloud_gpkg = os.path.join(output_folder, f"{item.id}_CLOUD_mask.gpkg")
                        resultado_vetor = vectorize_cloud_mask(temp_scl_path, cloud_gpkg)
                        
                        if resultado_vetor:
                            self.tiles_processed_output.append(f"✅ Vetor salvo: {cloud_gpkg}")
                            
                            # Carrega no QGIS
                            vector_layer = QgsVectorLayer(resultado_vetor, f"Nuvens {tile}", "ogr")
                            if vector_layer.isValid():
                                QgsProject.instance().addMapLayer(vector_layer)
                                self.tiles_processed_output.append(f"✅ Camada de nuvens adicionada à visualização.")
                            else:
                                self.tiles_processed_output.append(f"❌ Falha ao carregar o vetor no QGIS.")
                        else:
                            self.tiles_processed_output.append(f"⚠️ Nenhuma nuvem encontrada ou erro na vetorização.")
                        
                        # DELETA o arquivo TIF temporário da banda SCL
                        try:
                            # Aguarda os objetos perderem a referência antes de apagar
                            import gc; gc.collect() 
                            os.remove(temp_scl_path)
                            self.tiles_processed_output.append(f"🗑️ Arquivo SCL temporário apagado com sucesso.")
                        except Exception as e:
                            self.tiles_processed_output.append(f"⚠️ Não foi possível remover SCL temporária: {e}")
                            
                    else:
                        self.tiles_processed_output.append(f"❌ Erro ao baixar SCL. Código HTTP: {response.status_code}")
                else:
                    self.tiles_processed_output.append(f"⚠️ Banda SCL não disponível para {tile}.")
                
                QtWidgets.QApplication.processEvents()
            
            except Exception as e:
                self.tiles_processed_output.append(f"❌ Erro no processamento do tile {tile}: {str(e)}")
                
        self.tiles_processed_output.append(">>> Processo Opção 4 Concluído! <<<")
    
        #Função para GERAR O RGB DAS OPÇÕES 1 e 2 tratando a lista de tiles de entrada
    def process_rgb(self, band_order):
        tiles_input = self.tile_input.text().strip() # 'strip' remove espaços extras antes e depois da string
        tiles = [tile.strip() for tile in re.split(r'[,; ]+', tiles_input) if tile.strip()] ## Divide a string 'tiles_input' usando expressões regulares ,; e remove espaço
        # --- TRATAMENTO DA DATA ---
        raw_date = self.date_input.text().strip()
        if "/" in raw_date:
            partes = raw_date.split("/")
            if len(partes) == 3:
                date_str = f"{partes[2]}{partes[1]}{partes[0]}"
            else:
                date_str = ""
        else:
            date_str = raw_date
        # -------------------------- # # Obtém o valor da entrada de texto 'date_input',
        folder = self.folder_input.text()  # Obtém o caminho do diretório de saída

        # Verifica dados do campo de TILES BDC
        if not tiles:    
            QtWidgets.QMessageBox.warning(self, "Erro", "Por favor, informe o(s) Tile(s)!")
            return
            
        # Validação de formato BBBPPP para os Tiles BDC (ex: 028032)
        tiles_validos = [tile for tile in tiles if re.match(r"^\d{6}$", tile)]
        tiles_invalidos = [tile for tile in tiles if not re.match(r"^\d{6}$", tile)]

        # Exibir msg de erro casso formato seja invalido
        if tiles_invalidos:
            QtWidgets.QMessageBox.warning(
                self, 
                "Erro", 
                f"Tile(s) inválido(s) detectado(s): {', '.join(tiles_invalidos)}\nUse o formato BBBPPP (ex: 028032)."
            )
            return
     
        # Verifica dados do campo de DATA
        if not date_str:
            QtWidgets.QMessageBox.warning(self, "Erro", "Por favor, informe a Data!")
            return
            
        # Verifica dados do campo de PASTA DESTINO    
        if not folder:
            QtWidgets.QMessageBox.warning(self, "Erro", "Por favor, informe o Diretório!!")
            return    
        
        # Verifica FORMATO dados do campo de DATA           
        if not date_str or len(date_str) != 8 or not date_str.isdigit():
            QtWidgets.QMessageBox.warning(self, "Erro", "Informe a data no formato DD/MM/AAAA ou AAAAMMDD!")
            return
        
        # Verifica se Diretorio ja existe ,  senão cria ele
        if not os.path.exists(folder):
            os.makedirs(folder)

        #Recupera variaveis separadas de data
        year, month, day = date_str[:4], date_str[4:6], date_str[6:8]

        
        # Verifica a opção de normalização escolhida na interface (padrão: dado original)
        normalize = self.normalize_checkbox.isChecked()

        
        #Realiza a Busca
        for tile in tiles:
            bbb, ppp = tile[:3], tile[3:]
            url_base = f"https://data.inpe.br/bdc/data/s2-16d/v2/{bbb}/{ppp}/{year}/{month}/{day}/S2-16D_V2_{bbb}{ppp}_{date_str}"
            

            band_data = {}
            projection, geotransform = None, None
            for band_key in band_order:
                url = f"{url_base}_{band_key}.tif" #Configura URL para download
                base_name = url.split('/')[-1]
                self.tiles_processed_output.append(f"Baixando: {base_name}")
                QApplication.processEvents() # Atualiza mensagem de Processamentos 

                
                
                # Chama o método 'download_file_to_memory' com URL fornecida e carregar seu conteúdo na memória.               
                band_bytes = self.download_file_to_memory(url)
                
                #Verifica se há arquivos para a DATA INFORMADA
                if band_bytes is None:
                    self.tiles_processed_output.append(f"❌ Não existe dados para a data informada!")
                    return
                #Se SIM  continua processo    
                vsimem_path = f"/vsimem/{tile}_{band_key}.tif" # Cria caminho p/ arquivo virtual em memória com string formatada nome do tile e a chave do band
                gdal.FileFromMemBuffer(vsimem_path, band_bytes.getvalue()) # carregar os dados do arquivo TIFF na memória
                ds = gdal.Open(vsimem_path)# Abre o arquivo virtual que foi carregado na memória como se fosse um arquivo físico
                band_data[band_key] = ds.GetRasterBand(1).ReadAsArray() # Lê os dados da primeira banda (raster band) do arquivo TIFF
                if band_key == band_order[0]:
                    projection = ds.GetProjection()
                    geotransform = ds.GetGeoTransform() # coleta a projeção e a transformação geoespacial do arquivo.

            # Cria o nome do arquivo de saída para o raster, incorporando o tile, a data e a ordem das bandas no nome
            sufixo = "8bit" if normalize else "orig"
            output_name = f"S2-16D_V2_{tile}_{date_str}_{''.join(band_order)}_{sufixo}.tif"
            
            # Define o caminho completo onde o arquivo de saída será salvo, combinando o diretório de destino (folder) e o nome do arquivo
            output_path = os.path.join(folder, output_name)
            
            # Cria o nome da camada de saída que será usada no QGIS, novamente incorporando o tile, a data e as bandas
            output_layer = f"S2-16D_V2_{tile}_{date_str}_{''.join(band_order)}_{sufixo}"

            
            # A função recebe os dados das bandas, o caminho do arquivo de saída, a projeção e a transformação geoespacial
            self.create_rgb(
                band_data[band_order[0]], # R
                band_data[band_order[1]], # G
                band_data[band_order[2]], # B
                output_path, projection, geotransform,
                normalize=normalize
            )

            
            # Cria uma camada raster no QGIS a partir do arquivo TIFF gerado e atribui um nome à camada
            raster_layer = QgsRasterLayer(output_path, output_layer)
            
            if raster_layer.isValid():
                QgsProject.instance().addMapLayer(raster_layer)
                self.tiles_processed_output.append(f"✅ RGB criado: {output_name}")
                self.tiles_processed_output.append(f"-------------------------------------------------")
                self.tiles_processed_output.append(f"") # # Atualiza mensagem de Processamentos  com textos , pontilhados e linhas em branco
            else:
                self.tiles_processed_output.append(f"❌ Falha ao carregar camada: {tile}")
                self.tiles_processed_output.append(f"-------------------------------------------------")
                self.tiles_processed_output.append(f"")# # Atualiza mensagem de Processamentos  com textos , pontilhados e linhas em branco
                
            for band_key in band_order:
                gdal.Unlink(f"/vsimem/{tile}_{band_key}.tif") # DELETAR DADOS DA MEMORIA

        QtWidgets.QMessageBox.information(self, "Finalização", "Geração de RGB Concluído!")
                

#Função para GERAR O RGB DA OPÇÃO 3 ( Bandas escolhidas na Interface)
    def download_selected_bands(self):
        tiles_input = self.tile_input.text().strip() # recebe TXT de Tiles
        tiles = [tile.strip() for tile in re.split(r'[,; ]+', tiles_input) if tile.strip()] ## Divide a string 'tiles_input' usando expressões regulares ,; e remove espaço
        # --- TRATAMENTO DA DATA ---
        raw_date = self.date_input.text().strip()
        if "/" in raw_date:
            partes = raw_date.split("/")
            if len(partes) == 3:
                date_str = f"{partes[2]}{partes[1]}{partes[0]}"
            else:
                date_str = ""
        else:
            date_str = raw_date
        # --------------------------
        folder = self.folder_input.text() # Recebe Diretorio
        selected_bands = [b for b, cb in self.band_checkboxes.items() if cb.isChecked()] # define quais bandas usar de acordo com checkbox da interface

        #Verifica Tiles, Data , Diretorio e Bandas
        if not tiles or not date_str or not folder or not selected_bands:
            QtWidgets.QMessageBox.warning(self, "Erro", "Preencha todos os campos e selecione ao menos uma banda!")
            return
        #Verifica se diretorio de saida ja existe senão cria ele
        if not os.path.exists(folder):
            os.makedirs(folder)

        #Recupera variaveis separadas de data
        year, month, day = date_str[:4], date_str[4:6], date_str[6:8]
        
        # Validação de formato BBBPPP (ex: 028032)
        tiles_validos = [tile for tile in tiles if re.match(r"^\d{6}$", tile)]
        tiles_invalidos = [tile for tile in tiles if not re.match(r"^\d{6}$", tile)]

        if tiles_invalidos:
            QtWidgets.QMessageBox.warning(
                self, 
                "Erro", 
                f"Tile(s) inválido(s) detectado(s): {', '.join(tiles_invalidos)}\nUse o formato BBBPPP (ex: 028032)."
            )
            return
 
        #Realiza a Busca
        for tile in tiles:
            bbb, ppp = tile[:3], tile[3:]
            url_base = f"https://data.inpe.br/bdc/data/s2-16d/v2/{bbb}/{ppp}/{year}/{month}/{day}/S2-16D_V2_{bbb}{ppp}_{date_str}"

            for band_key in selected_bands:
                url = f"{url_base}_{band_key}.tif"  #Configura URL para download
                base_name = url.split('/')[-1]
                self.tiles_processed_output.append(f"Baixando: {base_name}")
                QApplication.processEvents() # Atualiza mensagem de Processamentos

                band_bytes = self.download_file_to_memory(url)
                
                #Verifica se há arquivos para a DATA INFORMADA
                if band_bytes is None:
                    self.tiles_processed_output.append(f"❌ Não existe dados para a data informada!")
                    continue
                    
                #Se SIM  continua processo PARA SALVAR OS TIFS DAS BANDAS
                output_path = os.path.join(folder, base_name)
                with open(output_path, 'wb') as f:
                    f.write(band_bytes.getvalue())
                raster_layer = QgsRasterLayer(output_path, base_name)
                if raster_layer.isValid():
                    QgsProject.instance().addMapLayer(raster_layer)
                    self.tiles_processed_output.append(f"✅ Banda salva: {base_name}")
                    self.tiles_processed_output.append(f"-------------------------------------------------")
                    self.tiles_processed_output.append(f"")
                else:
                    self.tiles_processed_output.append(f"❌ Falha ao carregar: {base_name}")
        
        QtWidgets.QMessageBox.information(self, "Finalização", "Download de Bandas Concluído!")
        
#-------------------------------  FIM DA FUNÇÃO PRINCIPAL  ------------------------------------        
    
    #Função para NORMALIZAR DADOS DE SAIDA em 8 BITS
    def normalize_to_8bit(self, array):
        array_min = np.min(array)
        array_max = np.max(array)
        return (((array - array_min) / (array_max - array_min)) * 255).astype(np.uint8)

    #Função para criar os RGB de 8 bits
    def create_rgb(self, r, g, b, output_path, projection, geotransform, normalize=False):
        driver = gdal.GetDriverByName('GTiff')
 
        if normalize:
            # Modo normalizado: converte cada banda para 8 bits (0-255)
            out_dtype = gdal.GDT_Byte
        else:
            # Modo original: mantém o tipo de dado nativo das bandas (ex: UInt16 do Sentinel-2)
            out_dtype = gdal_array.NumericTypeCodeToGDALTypeCode(r.dtype)
 
        out_ds = driver.Create(output_path, r.shape[1], r.shape[0], 3, out_dtype)
        out_ds.SetProjection(projection)
        out_ds.SetGeoTransform(geotransform)
 
        if normalize:
            out_ds.GetRasterBand(1).WriteArray(self.normalize_to_8bit(r))
            out_ds.GetRasterBand(2).WriteArray(self.normalize_to_8bit(g))
            out_ds.GetRasterBand(3).WriteArray(self.normalize_to_8bit(b))
        else:
            out_ds.GetRasterBand(1).WriteArray(r)
            out_ds.GetRasterBand(2).WriteArray(g)
            out_ds.GetRasterBand(3).WriteArray(b)
 
        out_ds.FlushCache()
        out_ds = None

    #Função para Processar em Memoria
    def download_file_to_memory(self, url):
        try:
            with requests.get(url, stream=True) as r:
                r.raise_for_status()
                return BytesIO(r.content)
        except Exception as e:
            print(f"Erro ao baixar: {e}")
            return None

    #Função BUSCA STAC
    def search_stac_item(self, tile, date_str):
    
        client = pystac_client.Client.open("https://data.inpe.br/bdc/stac/v1/")

        date_iso = datetime.strptime(date_str, "%Y%m%d").strftime("%Y-%m-%d")
        datetime_range = f"{date_iso}T00:00:00Z/{date_iso}T23:59:59Z"

        search = client.search(
            collections=["S2-16D-2"],
            query={"bdc:tile": {"eq": tile}},
            datetime=datetime_range,
            limit=1
        )

        items = list(search.get_items())
        return items[0] if items else None


#Função GERAR VRT
    def process_rgb_stac_vrt(self):
        tiles_input = self.tile_input.text().strip()
        tiles = [tile.strip() for tile in re.split(r'[,; ]+', tiles_input) if tile.strip()]
        # --- TRATAMENTO DA DATA ---
        raw_date = self.date_input.text().strip()
        if "/" in raw_date:
            partes = raw_date.split("/")
            if len(partes) == 3:
                date_str = f"{partes[2]}{partes[1]}{partes[0]}" # Converte DD/MM/AAAA para AAAAMMDD
            else:
                date_str = ""
        else:
            date_str = raw_date
        # --------------------------
        folder = self.folder_input.text()

        # Validações
        if not tiles:
            QtWidgets.QMessageBox.warning(self, "Erro", "Informe ao menos um Tile!")
            return
            
        # Verifica FORMATO dados do campo de DATA
        if len(date_str) != 8 or not date_str.isdigit():
            QtWidgets.QMessageBox.warning(self, "Erro", "A data deve ter o formato DD/MM/AAAA ou AAAAMMDD!")
            return

        if not folder:
            QtWidgets.QMessageBox.warning(self, "Erro", "Informe o diretório de saída!")
            return

        if not os.path.exists(folder):
            os.makedirs(folder)

        bandas = ["B11", "B08", "B04"]
        bandas_str = "_".join(bandas)

        for tile in tiles:
            self.tiles_processed_output.append(f"🔍 Buscando STAC para tile {tile} - data {date_str}")
            QApplication.processEvents()

            item = self.search_stac_item(tile, date_str)

            if not item:
                self.tiles_processed_output.append(f"❌ Nenhum item encontrado para {tile} em {date_str}")
                continue

            hrefs = []
            for band in bandas:
                if band not in item.assets:
                    self.tiles_processed_output.append(f"⚠️ Banda {band} não disponível para {tile}")
                    continue
                hrefs.append(f"/vsicurl/{item.assets[band].href}")

            if len(hrefs) != 3:
                self.tiles_processed_output.append(f"❌ Bandas incompletas para {tile}")
                continue

            vrt_name = f"S2-16D_V2_{tile}_{date_str}.vrt"
            vrt_path = os.path.join(folder, vrt_name)

            vrt = gdal.BuildVRT(
                vrt_path,
                hrefs,
                options=gdal.BuildVRTOptions(
                    separate=True,
                    addAlpha=False
                )
            )

            if vrt:
                vrt = None
                layer = QgsRasterLayer(vrt_path, vrt_name)
                if layer.isValid():
                    QgsProject.instance().addMapLayer(layer)
                    self.tiles_processed_output.append(f"✅ VRT criado: {vrt_name}")
                    self.tiles_processed_output.append("-------------------------------------------------")
                else:
                    self.tiles_processed_output.append(f"❌ Falha ao carregar VRT: {vrt_name}")
            else:
                self.tiles_processed_output.append(f"❌ Erro ao criar VRT para {tile}")

        
        QtWidgets.QMessageBox.information(self, "Finalização", "Geração de VRT Concluído!")


#-------------------------------FIM PROCESSOS ABA 1------------------------------------



#-------------------------------PROCESSO ABA 2------------------------------------
    
    #Função para varrer e Listar as datas de BDC disponiveis no periodo informado
    def buscar_datas_validas(self):
        tile = self.tile_check_input.text().strip()
        if not tile or len(tile) != 6:
            QtWidgets.QMessageBox.warning(self, "Erro", "Informe um tile válido (formato BBBPPP)")
            return

        data_inicial = self.date_start.date().toPyDate()
        data_final = self.date_end.date().toPyDate()

        if data_inicial > data_final:
            QtWidgets.QMessageBox.warning(self, "Erro", "A data inicial deve ser anterior à final")
            return

        self.output_datas_validas.clear()
        self.output_datas_validas.append(
            f"🔍 Buscando datas disponíveis (STAC) para o tile {tile} entre {data_inicial.strftime('%d/%m/%Y')} e {data_final.strftime('%d/%m/%Y')}..."
        )

        # ---------------- STAC ----------------
        client = pystac_client.Client.open(
            "https://data.inpe.br/bdc/stac/v1/"
        )

        datetime_range = (
            f"{data_inicial.isoformat()}T00:00:00Z/"
            f"{data_final.isoformat()}T23:59:59Z"
        )

        search = client.search(
            collections=["S2-16D-2"],
            query={"bdc:tile": {"eq": tile}},
            datetime=datetime_range
        )

        items = list(search.get_items())

        # --------------------------------------

        datas_validas = sorted({
            item.datetime.strftime("%Y%m%d")
            for item in items
        })
       
        for date_str in datas_validas:
            # Fatiando a string YYYYMMDD para montar DD/MM/YYYY
            data_br = f"{date_str[6:8]}/{date_str[4:6]}/{date_str[0:4]}"
            
            self.output_datas_validas.append(f"✅ {data_br}")
            QtWidgets.QApplication.processEvents()

        self.output_datas_validas.append(
            f"\n📅 Total de datas encontradas: {len(datas_validas)}"
        )

        
#-------------------------------FIM PROCESSO ABA 2------------------------------------



#--------------------------------- PARTE ESPECIFICA PARA PLUGIN---------------------------------------


#QGIS carrega o plugin automaticamente por meio da função classFactory
#O initGui() registra o plugin no menu e na barra de ferramentas do QGIS.
def classFactory(iface):
    return BDC_downloader_S216D(iface)


class BDC_downloader_S216D:
    def __init__(self, iface):
        self.iface = iface
        self.dialog = None

    def initGui(self):
        icon_path = os.path.join(os.path.dirname(__file__), "icon.png")
        self.action = QAction(QIcon(icon_path), "BDC SENTINEL 16 DIAS", self.iface.mainWindow())
        self.action.triggered.connect(self.run)
        self.iface.addPluginToMenu("BiomasBR - Amazônia", self.action)
        self.iface.addToolBarIcon(self.action)

    def unload(self):
        self.iface.removePluginMenu("BiomasBR - Amazônia", self.action)
        self.iface.removeToolBarIcon(self.action)

    def run(self):
        if self.dialog is None:
            self.dialog = BDCDialog()
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()
