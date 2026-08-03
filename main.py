import os
import re
import numpy as np
import requests

import pystac_client

from qgis.PyQt import QtWidgets
from qgis.core import QgsRasterLayer, QgsVectorLayer, QgsProject
from PyQt5.QtWidgets import QApplication, QTabWidget, QAction
from PyQt5.QtCore import Qt

from osgeo import gdal, ogr, osr, gdal_array
from io import BytesIO

from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta

from scipy import ndimage
from scipy.ndimage import uniform_filter

from qgis.PyQt.QtGui import QIcon
#-----------------------------FIM Importação de Bibliotecas -----------------------------------------------------------------------------------------------------------

# ──────────────────────────────────────────────────────────────────────────────
# VETORIZAÇÃO DE NUVENS (SCL)
# ──────────────────────────────────────────────────────────────────────────────
def fill_holes_in_mask(mask):
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
        self.setWindowTitle("BDC Downloader - Sentinel-2/MSI L2A | LCF 16 days")
        self.setMinimumWidth(600)

        tabs = QtWidgets.QTabWidget()

        # ======================== ABA 1 ========================
        main_widget = QtWidgets.QWidget()
        main_layout = QtWidgets.QVBoxLayout()

        stac_label = QtWidgets.QLabel("<b>STAC API:</b> https://data.inpe.br/bdc/stac/v1/")
        stac_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        stac_label.setStyleSheet("color: #555555;")
        main_layout.addWidget(stac_label)

        self.tile_input = QtWidgets.QLineEdit()
        self.tile_input.setPlaceholderText("Ex: 003016,032010")
        main_layout.addWidget(QtWidgets.QLabel("Entre com a Lista de Tiles BDC separados por vírgulas ou espaços:"))
        main_layout.addWidget(self.tile_input)

        self.date_input = QtWidgets.QLineEdit()
        self.date_input.setPlaceholderText("Ex: DD/MM/AAAA")
        main_layout.addWidget(QtWidgets.QLabel("Informe a Data da Coleção (Formato: DD/MM/AAAA):"))
        main_layout.addWidget(self.date_input)

        self.folder_input = QtWidgets.QLineEdit()
        folder_layout = QtWidgets.QHBoxLayout()
        folder_button = QtWidgets.QPushButton("Escolher pasta")
        folder_button.clicked.connect(self.select_folder)
        folder_layout.addWidget(self.folder_input)
        folder_layout.addWidget(folder_button)
        main_layout.addWidget(QtWidgets.QLabel("Pasta de destino:"))
        main_layout.addLayout(folder_layout)

        self.normalize_checkbox = QtWidgets.QCheckBox("Normalizar RGB para 8 bits (padrão: manter dado original)")
        self.normalize_checkbox.setChecked(False)
        main_layout.addWidget(self.normalize_checkbox)

        self.download_button_alt = QtWidgets.QPushButton("Opção 1 >>>>>  Criar VRT da Composição RGB (R11_G08_B04) - Para Visualização Rápida")
        self.download_button_alt.clicked.connect(self.process_rgb_stac_vrt)
        main_layout.addWidget(self.download_button_alt)

        self.download_button = QtWidgets.QPushButton("Opção 2 >>>>>  Download Composição RGB em 8bits (R11_G08_B04) + Banda PROVENANCE")
        self.download_button.clicked.connect(self.executar_opcao_2_completa)
        main_layout.addWidget(self.download_button)

        self.band_checkboxes = {}
        bands = ["B01", "B02", "B03", "B04", "B05", "B06", "B07", "B08", "B8A", "B09", "B11", "B12", "NDVI", "EVI", "NBR", "SCL"]
        band_group = QtWidgets.QGroupBox("Selecionar bandas para download individual, caso deseje utilizar a opção 3:")
        band_layout = QtWidgets.QGridLayout()
        for i, band in enumerate(bands):
            checkbox = QtWidgets.QCheckBox(band)
            self.band_checkboxes[band] = checkbox
            band_layout.addWidget(checkbox, i // 4, i % 4)
        band_group.setLayout(band_layout)
        main_layout.addWidget(band_group)

        self.download_selected_button = QtWidgets.QPushButton("Opção 3 >>>>>     Download das bandas selecionadas acima")
        self.download_selected_button.clicked.connect(self.download_selected_bands)
        main_layout.addWidget(self.download_selected_button)

        self.download_cloud_button = QtWidgets.QPushButton("Opção 4 >>>>>  Gerar Máscara Vetorial de Nuvens")
        self.download_cloud_button.clicked.connect(self.executar_opcao_4_nuvens)
        main_layout.addWidget(self.download_cloud_button)

        self.tiles_processed_output = QtWidgets.QTextEdit()
        self.tiles_processed_output.setReadOnly(True)
        main_layout.addWidget(QtWidgets.QLabel("Logs de Processamento:"))
        main_layout.addWidget(self.tiles_processed_output)

        main_widget.setLayout(main_layout)
        tabs.addTab(main_widget, "Download e Geração de Dados")

        # ======================== ABA 2 ========================
        data_widget = QtWidgets.QWidget()
        data_layout = QtWidgets.QVBoxLayout()

        self.tile_check_input = QtWidgets.QLineEdit()
        self.tile_check_input.setPlaceholderText("Ex: 027022")
        data_layout.addWidget(QtWidgets.QLabel("Informe um Tile BDC para realizar a busca (formato BBBPPP):"))
        data_layout.addWidget(self.tile_check_input)

        data_layout.addWidget(QtWidgets.QLabel(""))
        data_layout.addWidget(QtWidgets.QLabel("Esta coleção possui dados desde 01/01/2017 com um intervalo de 16 dias entre cada mosaico."))
        data_layout.addWidget(QtWidgets.QLabel(""))

        date_range_layout = QtWidgets.QHBoxLayout()
        self.date_start = QtWidgets.QDateEdit()
        self.date_start.setCalendarPopup(True)
        self.date_start.setDisplayFormat("dd/MM/yyyy")
        self.date_start.setDate(datetime.today().replace(month=1, day=1))

        self.date_end = QtWidgets.QDateEdit()
        self.date_end.setCalendarPopup(True)
        self.date_end.setDisplayFormat("dd/MM/yyyy")
        self.date_end.setDate(datetime.today())

        date_range_layout.addWidget(QtWidgets.QLabel("Data Inicial:"))
        date_range_layout.addWidget(self.date_start)
        date_range_layout.addWidget(QtWidgets.QLabel("Data Final:"))
        date_range_layout.addWidget(self.date_end)
        data_layout.addLayout(date_range_layout)

        check_button = QtWidgets.QPushButton("🔍 Buscar Datas no Servidor")
        check_button.clicked.connect(self.buscar_datas_validas)
        data_layout.addWidget(check_button)

        self.output_datas_validas = QtWidgets.QTextEdit()
        self.output_datas_validas.setReadOnly(True)
        data_layout.addWidget(QtWidgets.QLabel("Datas válidas encontradas:"))
        data_layout.addWidget(self.output_datas_validas)

        data_widget.setLayout(data_layout)
        tabs.addTab(data_widget, "Pesquisa de Datas da Coleção")

        # ======================== ABA 3: IMAGEM RADAR ========================
        radar_widget = QtWidgets.QWidget()
        radar_layout = QtWidgets.QVBoxLayout()

        radar_layout.addWidget(QtWidgets.QLabel("<b>Geração de mosaico temporal Sentinel-1 VH</b>"))

        radar_layout.addWidget(QtWidgets.QLabel("Ano de referência:"))
        self.radar_year_input = QtWidgets.QLineEdit()
        self.radar_year_input.setPlaceholderText("Ex: 2025")
        radar_layout.addWidget(self.radar_year_input)

        radar_layout.addWidget(QtWidgets.QLabel("Tile BDC (formato BBBPPP):"))
        self.radar_tile_input = QtWidgets.QLineEdit()
        self.radar_tile_input.setPlaceholderText("Ex: 023009")
        radar_layout.addWidget(self.radar_tile_input)

        folder_radar_layout = QtWidgets.QHBoxLayout()
        self.radar_folder_input = QtWidgets.QLineEdit()
        folder_radar_button = QtWidgets.QPushButton("Escolher pasta")
        folder_radar_button.clicked.connect(self.select_radar_folder)
        folder_radar_layout.addWidget(self.radar_folder_input)
        folder_radar_layout.addWidget(folder_radar_button)
        radar_layout.addWidget(QtWidgets.QLabel("Pasta de destino:"))
        radar_layout.addLayout(folder_radar_layout)

        self.radar_process_button = QtWidgets.QPushButton("Gerar Imagem Radar")
        self.radar_process_button.clicked.connect(self.process_radar_image)
        radar_layout.addWidget(self.radar_process_button)

        self.radar_log_output = QtWidgets.QTextEdit()
        self.radar_log_output.setReadOnly(True)
        radar_layout.addWidget(QtWidgets.QLabel("Logs de Processamento:"))
        radar_layout.addWidget(self.radar_log_output)

        radar_widget.setLayout(radar_layout)
        tabs.addTab(radar_widget, "Imagem Radar")

        # Layout principal
        main_layout = QtWidgets.QVBoxLayout()
        main_layout.addWidget(tabs)
        self.setLayout(main_layout)

    # ======================== MÉTODOS DA ABA 1 ========================
    def select_folder(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Selecione a pasta de destino")
        if folder:
            self.folder_input.setText(folder)

    def executar_opcao_2_completa(self):
        self.process_rgb(["B11", "B08", "B04"])
        self.tiles_processed_output.append("\n--- Iniciando download da banda PROVENANCE ---")
        QtWidgets.QApplication.processEvents()
        
        tiles_text = self.tile_input.text()
        raw_date = self.date_input.text()
        output_folder = self.folder_input.text()

        if not tiles_text or not raw_date or not output_folder:
            return

        if "/" in raw_date:
            try:
                dia, mes, ano = raw_date.split("/")
                data = f"{ano}{mes}{dia}"
            except ValueError:
                return 
        else:
            data = raw_date

        tiles = [t.strip() for t in re.split(r'[,\s]+', tiles_text) if t.strip()]

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
                    continue

                item = items[0]
                band = "PROVENANCE"
                if band in item.assets:
                    url = item.assets[band].href
                    self.tiles_processed_output.append(f"Baixando {band} para o tile {tile}...")
                    QtWidgets.QApplication.processEvents()
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
                    response = requests.get(url)
                    if response.status_code == 200:
                        temp_scl_path = os.path.join(output_folder, f"temp_{item.id}_{band}.tif")
                        with open(temp_scl_path, "wb") as f:
                            f.write(response.content)
                        self.tiles_processed_output.append("Vetorizando nuvens...")
                        QtWidgets.QApplication.processEvents()
                        cloud_gpkg = os.path.join(output_folder, f"{item.id}_CLOUD_mask.gpkg")
                        resultado_vetor = vectorize_cloud_mask(temp_scl_path, cloud_gpkg)
                        if resultado_vetor:
                            self.tiles_processed_output.append(f"✅ Vetor salvo: {cloud_gpkg}")
                            vector_layer = QgsVectorLayer(resultado_vetor, f"Nuvens {tile}", "ogr")
                            if vector_layer.isValid():
                                QgsProject.instance().addMapLayer(vector_layer)
                                self.tiles_processed_output.append(f"✅ Camada de nuvens adicionada à visualização.")
                            else:
                                self.tiles_processed_output.append(f"❌ Falha ao carregar o vetor no QGIS.")
                        else:
                            self.tiles_processed_output.append(f"⚠️ Nenhuma nuvem encontrada ou erro na vetorização.")
                        try:
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

    def process_rgb(self, band_order):
        tiles_input = self.tile_input.text().strip()
        tiles = [tile.strip() for tile in re.split(r'[,; ]+', tiles_input) if tile.strip()]
        raw_date = self.date_input.text().strip()
        if "/" in raw_date:
            partes = raw_date.split("/")
            if len(partes) == 3:
                date_str = f"{partes[2]}{partes[1]}{partes[0]}"
            else:
                date_str = ""
        else:
            date_str = raw_date
        folder = self.folder_input.text()

        if not tiles:
            QtWidgets.QMessageBox.warning(self, "Erro", "Por favor, informe o(s) Tile(s)!")
            return
        tiles_validos = [tile for tile in tiles if re.match(r"^\d{6}$", tile)]
        tiles_invalidos = [tile for tile in tiles if not re.match(r"^\d{6}$", tile)]
        if tiles_invalidos:
            QtWidgets.QMessageBox.warning(self, "Erro", f"Tile(s) inválido(s) detectado(s): {', '.join(tiles_invalidos)}\nUse o formato BBBPPP (ex: 028032).")
            return
        if not date_str:
            QtWidgets.QMessageBox.warning(self, "Erro", "Por favor, informe a Data!")
            return
        if not folder:
            QtWidgets.QMessageBox.warning(self, "Erro", "Por favor, informe o Diretório!!")
            return
        if not date_str or len(date_str) != 8 or not date_str.isdigit():
            QtWidgets.QMessageBox.warning(self, "Erro", "Informe a data no formato DD/MM/AAAA ou AAAAMMDD!")
            return
        if not os.path.exists(folder):
            os.makedirs(folder)

        year, month, day = date_str[:4], date_str[4:6], date_str[6:8]
        normalize = self.normalize_checkbox.isChecked()

        for tile in tiles:
            bbb, ppp = tile[:3], tile[3:]
            url_base = f"https://data.inpe.br/bdc/data/s2-16d/v2/{bbb}/{ppp}/{year}/{month}/{day}/S2-16D_V2_{bbb}{ppp}_{date_str}"
            band_data = {}
            projection, geotransform = None, None
            for band_key in band_order:
                url = f"{url_base}_{band_key}.tif"
                base_name = url.split('/')[-1]
                self.tiles_processed_output.append(f"Baixando: {base_name}")
                QApplication.processEvents()
                band_bytes = self.download_file_to_memory(url)
                if band_bytes is None:
                    self.tiles_processed_output.append(f"❌ Não existe dados para a data informada!")
                    return
                vsimem_path = f"/vsimem/{tile}_{band_key}.tif"
                gdal.FileFromMemBuffer(vsimem_path, band_bytes.getvalue())
                ds = gdal.Open(vsimem_path)
                band_data[band_key] = ds.GetRasterBand(1).ReadAsArray()
                if band_key == band_order[0]:
                    projection = ds.GetProjection()
                    geotransform = ds.GetGeoTransform()

            sufixo = "8bit" if normalize else "orig"
            output_name = f"S2-16D_V2_{tile}_{date_str}_{''.join(band_order)}_{sufixo}.tif"
            output_path = os.path.join(folder, output_name)
            output_layer = f"S2-16D_V2_{tile}_{date_str}_{''.join(band_order)}_{sufixo}"

            self.create_rgb(
                band_data[band_order[0]],
                band_data[band_order[1]],
                band_data[band_order[2]],
                output_path, projection, geotransform,
                normalize=normalize
            )
            raster_layer = QgsRasterLayer(output_path, output_layer)
            if raster_layer.isValid():
                QgsProject.instance().addMapLayer(raster_layer)
                self.tiles_processed_output.append(f"✅ RGB criado: {output_name}")
                self.tiles_processed_output.append(f"-------------------------------------------------")
                self.tiles_processed_output.append(f"")
            else:
                self.tiles_processed_output.append(f"❌ Falha ao carregar camada: {tile}")
                self.tiles_processed_output.append(f"-------------------------------------------------")
                self.tiles_processed_output.append(f"")
            for band_key in band_order:
                gdal.Unlink(f"/vsimem/{tile}_{band_key}.tif")
        QtWidgets.QMessageBox.information(self, "Finalização", "Geração de RGB Concluído!")

    def download_selected_bands(self):
        tiles_input = self.tile_input.text().strip()
        tiles = [tile.strip() for tile in re.split(r'[,; ]+', tiles_input) if tile.strip()]
        raw_date = self.date_input.text().strip()
        if "/" in raw_date:
            partes = raw_date.split("/")
            if len(partes) == 3:
                date_str = f"{partes[2]}{partes[1]}{partes[0]}"
            else:
                date_str = ""
        else:
            date_str = raw_date
        folder = self.folder_input.text()
        selected_bands = [b for b, cb in self.band_checkboxes.items() if cb.isChecked()]

        if not tiles or not date_str or not folder or not selected_bands:
            QtWidgets.QMessageBox.warning(self, "Erro", "Preencha todos os campos e selecione ao menos uma banda!")
            return
        if not os.path.exists(folder):
            os.makedirs(folder)

        year, month, day = date_str[:4], date_str[4:6], date_str[6:8]
        tiles_validos = [tile for tile in tiles if re.match(r"^\d{6}$", tile)]
        tiles_invalidos = [tile for tile in tiles if not re.match(r"^\d{6}$", tile)]
        if tiles_invalidos:
            QtWidgets.QMessageBox.warning(self, "Erro", f"Tile(s) inválido(s) detectado(s): {', '.join(tiles_invalidos)}\nUse o formato BBBPPP (ex: 028032).")
            return

        for tile in tiles:
            bbb, ppp = tile[:3], tile[3:]
            url_base = f"https://data.inpe.br/bdc/data/s2-16d/v2/{bbb}/{ppp}/{year}/{month}/{day}/S2-16D_V2_{bbb}{ppp}_{date_str}"
            for band_key in selected_bands:
                url = f"{url_base}_{band_key}.tif"
                base_name = url.split('/')[-1]
                self.tiles_processed_output.append(f"Baixando: {base_name}")
                QApplication.processEvents()
                band_bytes = self.download_file_to_memory(url)
                if band_bytes is None:
                    self.tiles_processed_output.append(f"❌ Não existe dados para a data informada!")
                    continue
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

    def normalize_to_8bit(self, array):
        array_min = np.min(array)
        array_max = np.max(array)
        return (((array - array_min) / (array_max - array_min)) * 255).astype(np.uint8)

    def create_rgb(self, r, g, b, output_path, projection, geotransform, normalize=False):
        driver = gdal.GetDriverByName('GTiff')
        if normalize:
            out_dtype = gdal.GDT_Byte
        else:
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

    def download_file_to_memory(self, url):
        try:
            with requests.get(url, stream=True) as r:
                r.raise_for_status()
                return BytesIO(r.content)
        except Exception as e:
            print(f"Erro ao baixar: {e}")
            return None

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

    def process_rgb_stac_vrt(self):
        tiles_input = self.tile_input.text().strip()
        tiles = [tile.strip() for tile in re.split(r'[,; ]+', tiles_input) if tile.strip()]
        raw_date = self.date_input.text().strip()
        if "/" in raw_date:
            partes = raw_date.split("/")
            if len(partes) == 3:
                date_str = f"{partes[2]}{partes[1]}{partes[0]}"
            else:
                date_str = ""
        else:
            date_str = raw_date
        folder = self.folder_input.text()

        if not tiles:
            QtWidgets.QMessageBox.warning(self, "Erro", "Informe ao menos um Tile!")
            return
        if len(date_str) != 8 or not date_str.isdigit():
            QtWidgets.QMessageBox.warning(self, "Erro", "A data deve ter o formato DD/MM/AAAA ou AAAAMMDD!")
            return
        if not folder:
            QtWidgets.QMessageBox.warning(self, "Erro", "Informe o diretório de saída!")
            return
        if not os.path.exists(folder):
            os.makedirs(folder)

        bandas = ["B11", "B08", "B04"]
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
            vrt = gdal.BuildVRT(vrt_path, hrefs, options=gdal.BuildVRTOptions(separate=True, addAlpha=False))
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

    # ======================== MÉTODOS DA ABA 2 ========================
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
        self.output_datas_validas.append(f"🔍 Buscando datas disponíveis (STAC) para o tile {tile} entre {data_inicial.strftime('%d/%m/%Y')} e {data_final.strftime('%d/%m/%Y')}...")
        client = pystac_client.Client.open("https://data.inpe.br/bdc/stac/v1/")
        datetime_range = f"{data_inicial.isoformat()}T00:00:00Z/{data_final.isoformat()}T23:59:59Z"
        search = client.search(
            collections=["S2-16D-2"],
            query={"bdc:tile": {"eq": tile}},
            datetime=datetime_range
        )
        items = list(search.get_items())
        datas_validas = sorted({item.datetime.strftime("%Y%m%d") for item in items})
        for date_str in datas_validas:
            data_br = f"{date_str[6:8]}/{date_str[4:6]}/{date_str[0:4]}"
            self.output_datas_validas.append(f"✅ {data_br}")
            QtWidgets.QApplication.processEvents()
        self.output_datas_validas.append(f"\n📅 Total de datas encontradas: {len(datas_validas)}")

    # ======================== MÉTODOS DA ABA 3 ========================
    def select_radar_folder(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Selecione a pasta de destino")
        if folder:
            self.radar_folder_input.setText(folder)

    def get_tile_bbox(self, tile):
        """
        Retorna o bbox do tile BDC usando dicionário local de coordenadas.
        Formato: [minx, miny, maxx, maxy] em EPSG:4326
        """
        # Dicionário com bboxes de todos os tiles BDC
        known_tiles = {
            "001014": [-74.87107, -8.019303, -73.83495, -7.00971],
            "002011": [-73.69892, -5.256667, -72.67687, -4.245657],
            "002012": [-73.7667, -6.200877, -72.74094, -5.191637],
            "002013": [-73.83495, -7.143474, -72.80545, -6.135744],
            "002014": [-73.90367, -8.084697, -72.8704, -7.078219],
            "002015": [-73.97287, -9.024786, -72.9358, -8.019303],
            "002016": [-74.04255, -9.963981, -73.00166, -8.959235],
            "003011": [-72.74094, -5.318451, -71.72171, -4.3106],
            "003012": [-72.80545, -6.262762, -71.78252, -5.256667],
            "003013": [-72.8704, -7.205475, -71.84375, -6.200877],
            "003014": [-72.9358, -8.146832, -71.9054, -7.143474],
            "003015": [-73.00166, -9.087071, -71.96747, -8.084697],
            "003016": [-73.06797, -10.02643, -72.02998, -9.024786],
            "004010": [-71.72171, -4.430759, -70.70899, -3.424068],
            "004011": [-71.78252, -5.376989, -70.76614, -4.372301],
            "004012": [-71.84375, -6.321396, -70.82369, -5.318451],
            "004013": [-71.9054, -7.264221, -70.88163, -6.262762],
            "004014": [-71.96747, -8.205704, -70.93997, -7.205475],
            "004015": [-72.02998, -9.146087, -70.99872, -8.146832],
            "004016": [-72.09292, -10.08561, -71.05787, -9.087071],
            "005004": [-70.42896, 1.243677, -69.44037, 2.264725],
            "005005": [-70.48421, 0.2813048, -69.49214, 1.298742],
            "005006": [-70.53984, -0.6777518, -69.54425, 0.3363573],
            "005007": [-70.59584, -1.633754, -69.59672, -0.6226966],
            "005009": [-70.70899, -3.537614, -69.70272, -2.531852],
            "005010": [-70.76614, -4.485973, -69.75627, -3.482462],
            "005011": [-70.82369, -5.43228, -69.81018, -4.430759],
            "005012": [-70.88163, -6.376778, -69.86445, -5.376989],
            "005013": [-70.93997, -7.319709, -69.91911, -6.321396],
            "005014": [-70.99872, -8.261314, -69.97414, -7.264221],
            "005015": [-71.05787, -9.201833, -70.02955, -8.205704],
            "005016": [-71.11744, -10.14151, -70.08535, -9.146087],
            "005017": [-71.17742, -11.08057, -70.14153, -10.08561],
            "006004": [-69.49214, 1.191852, -68.50632, 2.209632],
            "006005": [-69.54425, 0.2294909, -68.55497, 1.243677],
            "006006": [-69.59672, -0.729569, -68.60394, 0.2813048],
            "006007": [-69.64954, -1.685588, -68.65325, -0.6777518],
            "006008": [-69.70272, -2.638823, -68.70289, -1.633754],
            "006009": [-69.75627, -3.589525, -68.75286, -2.586957],
            "006010": [-69.81018, -4.537943, -68.80318, -3.537614],
            "006011": [-69.86445, -5.484323, -68.85384, -4.485973],
            "006012": [-69.91911, -6.428908, -68.90485, -5.43228],
            "006013": [-69.97414, -7.37194, -68.95621, -6.376778],
            "006014": [-70.02955, -8.31366, -69.00792, -7.319709],
            "006015": [-70.08535, -9.254308, -69.06, -8.261314],
            "006016": [-70.14153, -10.19412, -69.11243, -9.201833],
            "006017": [-70.19811, -11.13335, -69.16524, -10.14151],
            "007003": [-68.50632, 2.109174, -67.52672, 3.127552],
            "007004": [-68.55497, 1.143268, -67.57194, 2.157782],
            "007005": [-68.60394, 0.1809155, -67.61747, 1.191852],
            "007006": [-68.65325, -0.7781482, -67.6633, 0.2294909],
            "007007": [-68.70289, -1.734184, -67.70944, -0.729569],
            "007008": [-68.75286, -2.687449, -67.75589, -1.685588],
            "007009": [-68.80318, -3.638194, -67.80266, -2.638823],
            "007010": [-68.85384, -4.586669, -67.84975, -3.589525],
            "007011": [-68.90485, -5.533118, -67.89716, -4.537943],
            "007012": [-68.95621, -6.477785, -67.94489, -5.484323],
            "007013": [-69.00792, -7.420912, -67.99296, -6.428908],
            "007014": [-69.06, -8.36274, -68.04135, -7.37194],
            "007015": [-69.11243, -9.30351, -68.09008, -8.31366],
            "007016": [-69.16524, -10.24346, -68.13915, -9.254308],
            "007017": [-69.21841, -11.18284, -68.18857, -10.19412],
            "008003": [-67.57194, 2.063808, -66.59515, 3.078908],
            "008004": [-67.61747, 1.097922, -66.63727, 2.109174],
            "008005": [-67.6633, 0.1355787, -66.67967, 1.143268],
            "008006": [-67.70944, -0.8234892, -66.72235, 0.1809155],
            "008007": [-67.75589, -1.779542, -66.76532, -0.7781482],
            "008008": [-67.80266, -2.732835, -66.80859, -1.734184],
            "008009": [-67.84975, -3.683621, -66.85214, -2.687449],
            "008010": [-67.89716, -4.632148, -66.896, -3.638194],
            "008011": [-67.94489, -5.578663, -66.94015, -4.586669],
            "008012": [-67.99296, -6.523407, -66.98461, -5.533118],
            "008013": [-68.04135, -7.466624, -67.02937, -6.477785],
            "008014": [-68.09008, -8.408554, -67.07444, -7.420912],
            "008015": [-68.13915, -9.349437, -67.11983, -8.36274],
            "008016": [-68.18857, -10.28952, -67.16553, -9.30351],
            "008017": [-68.23833, -11.22903, -67.21155, -10.24346],
            "009005": [-66.72235, 0.09348018, -65.74158, 1.097922],
            "009006": [-66.76532, -0.865592, -65.78111, 0.1355787],
            "009007": [-66.80859, -1.82166, -65.82091, -0.8234892],
            "009008": [-66.85214, -2.774981, -65.86098, -1.779542],
            "009009": [-66.896, -3.725805, -65.90133, -2.732835],
            "009010": [-66.94015, -4.674382, -65.94194, -3.683621],
            "009011": [-66.98461, -5.620957, -65.98284, -4.632148],
            "009012": [-67.02937, -6.565774, -66.02402, -5.578663],
            "009013": [-67.07444, -7.509075, -66.06548, -6.523407],
            "009014": [-67.11983, -8.4511, -66.10722, -7.466624],
            "009015": [-67.16553, -9.39209, -66.14926, -8.408554],
            "009016": [-67.21155, -10.33229, -66.19159, -9.349437],
            "009017": [-67.2579, -11.27193, -66.23422, -10.28952],
            "010001": [-65.62455, 3.926154, -64.66011, 4.943222],
            "010004": [-65.74158, 1.016951, -64.76709, 2.021684],
            "010005": [-65.78111, 0.05462009, -64.80323, 1.055817],
            "010006": [-65.82091, -0.9044566, -64.83961, 0.09348018],
            "010007": [-65.86098, -1.86054, -64.87623, -0.865592],
            "010008": [-65.90133, -2.813886, -64.91311, -1.82166],
            "010009": [-65.94194, -3.764746, -64.95023, -2.774981],
            "010010": [-65.98284, -4.71337, -64.98761, -3.725805],
            "010011": [-66.02402, -5.660001, -65.02525, -4.674382],
            "010012": [-66.06548, -6.604885, -65.06314, -5.620957],
            "010013": [-66.10722, -7.548263, -65.1013, -6.565774],
            "010014": [-66.14926, -8.490377, -65.13971, -7.509075],
            "010015": [-66.19159, -9.431466, -65.1784, -8.4511],
            "010016": [-66.23422, -10.37177, -65.21735, -9.39209],
            "010017": [-66.27714, -11.31154, -65.25658, -10.33229],
            "010018": [-66.32037, -12.25101, -65.29608, -11.27193],
            "011001": [-64.69553, 3.890451, -63.73393, 4.904222],
            "011002": [-64.73119, 2.916775, -63.76628, 3.926154],
            "011003": [-64.76709, 1.947158, -63.79884, 2.952442],
            "011004": [-64.80323, 0.9813235, -63.83163, 1.982801],
            "011005": [-64.83961, 0.01899837, -63.86463, 1.016951],
            "011006": [-64.87623, -0.9400828, -63.89786, 0.05462009],
            "011007": [-64.91311, -1.89618, -63.93131, -0.9044566],
            "011008": [-64.95023, -2.84955, -63.96498, -1.86054],
            "011009": [-64.98761, -3.800444, -63.99889, -2.813886],
            "011010": [-65.02525, -4.74911, -64.03303, -3.764746],
            "011011": [-65.06314, -5.695794, -64.0674, -4.71337],
            "011012": [-65.1013, -6.64074, -64.102, -5.660001],
            "011013": [-65.13971, -7.58419, -64.13685, -6.604885],
            "011014": [-65.1784, -8.526384, -64.17194, -7.548263],
            "011015": [-65.21735, -9.467565, -64.20727, -8.490377],
            "011016": [-65.25658, -10.40797, -64.24285, -9.431466],
            "011017": [-65.29608, -11.34785, -64.27867, -10.37177],
            "011018": [-65.33586, -12.28744, -64.31475, -11.31154],
            "011019": [-65.37593, -13.22698, -64.35108, -12.25101],
            "012001": [-63.76628, 3.857996, -62.80753, 4.868474],
            "012002": [-63.79884, 2.884351, -62.83681, 3.890451],
            "012003": [-63.83163, 1.914757, -62.86628, 2.916775],
            "012004": [-63.86463, 0.9489357, -62.89595, 1.947158],
            "012005": [-63.89786, -0.01338501, -62.92581, 0.9813235],
            "012006": [-63.93131, -0.9724706, -62.95588, 0.01899837],
            "012007": [-63.96498, -1.928581, -62.98615, -0.9400828],
            "012008": [-63.99889, -2.881973, -63.01663, -1.89618],
            "012009": [-64.03303, -3.832897, -63.04731, -2.84955],
            "012010": [-64.0674, -4.781603, -63.07821, -3.800444],
            "012011": [-64.102, -5.728335, -63.10931, -4.74911],
            "012012": [-64.13685, -6.673337, -63.14063, -5.695794],
            "012013": [-64.17194, -7.616852, -63.17216, -6.64074],
            "012014": [-64.20727, -8.559121, -63.20392, -7.58419],
            "012015": [-64.24285, -9.500385, -63.23589, -8.526384],
            "012016": [-64.27867, -10.44088, -63.26809, -9.467565],
            "012017": [-64.31475, -11.38086, -63.30051, -10.40797],
            "012018": [-64.35108, -12.32056, -63.33316, -11.34785],
            "012019": [-64.38767, -13.26023, -63.36604, -12.28744],
            "013001": [-62.83681, 3.828787, -61.88094, 4.835977],
            "013002": [-62.86628, 2.85517, -61.90714, 3.857996],
            "013003": [-62.89595, 1.885596, -61.93352, 2.884351],
            "013004": [-62.92581, 0.9197869, -61.96007, 1.914757],
            "013005": [-62.95588, -0.04253005, -61.98679, 0.9489357],
            "013006": [-62.98615, -1.00162, -62.0137, -0.01338501],
            "013007": [-63.01663, -1.957743, -62.04079, -0.9724706],
            "013008": [-63.04731, -2.911154, -62.06807, -1.928581],
            "013009": [-63.07821, -3.862106, -62.09553, -2.881973],
            "013010": [-63.10931, -4.810847, -62.12317, -3.832897],
            "013011": [-63.14063, -5.757623, -62.15101, -4.781603],
            "013012": [-63.17216, -6.702677, -62.17904, -5.728335],
            "013013": [-63.20392, -7.646251, -62.20726, -6.673337],
            "013014": [-63.23589, -8.588587, -62.23568, -7.616852],
            "013015": [-63.26809, -9.529926, -62.26429, -8.559121],
            "013016": [-63.30051, -10.47051, -62.2931, -9.500385],
            "013017": [-63.33316, -11.41058, -62.32212, -10.44088],
            "013018": [-63.36604, -12.35038, -62.35134, -11.38086],
            "013019": [-63.39915, -13.29015, -62.38076, -12.32056],
            "014001": [-61.90714, 3.802824, -60.95418, 4.806732],
            "014002": [-61.93352, 2.829233, -60.9773, 3.828787],
            "014003": [-61.96007, 1.859676, -61.00058, 2.85517],
            "014004": [-61.98679, 0.893877, -61.02401, 1.885596],
            "014005": [-62.0137, -0.06843676, -61.0476, 0.9197869],
            "014006": [-62.04079, -1.02753, -61.07134, -0.04253005],
            "014007": [-62.06807, -1.983664, -61.09525, -1.00162],
            "014008": [-62.09553, -2.937094, -61.11932, -1.957743],
            "014009": [-62.12317, -3.888071, -61.14355, -2.911154],
            "014010": [-62.15101, -4.836844, -61.16795, -3.862106],
            "014011": [-62.17904, -5.783658, -61.19252, -4.810847],
            "014012": [-62.20726, -6.728758, -61.21725, -5.757623],
            "014013": [-62.23568, -7.672385, -61.24216, -6.702677],
            "014014": [-62.26429, -8.614781, -61.26724, -7.646251],
            "014015": [-62.2931, -9.556187, -61.29249, -8.588587],
            "014016": [-62.32212, -10.49684, -61.31792, -9.529926],
            "014017": [-62.35134, -11.43699, -61.34353, -10.47051],
            "014018": [-62.38076, -12.37688, -61.36931, -11.41058],
            "014019": [-62.4104, -13.31676, -61.39528, -12.35038],
            "014020": [-62.44024, -14.25686, -61.42143, -13.29015],
            "015000": [-60.95418, 4.757991, -60.00736, 5.763264],
            "015001": [-60.9773, 3.780107, -60.02727, 4.780736],
            "015002": [-61.00058, 2.806537, -60.04731, 3.802824],
            "015003": [-61.02401, 1.836996, -60.06748, 2.829233],
            "015004": [-61.0476, 0.871206, -60.08779, 1.859676],
            "015005": [-61.07134, -0.09110515, -60.10824, 0.893877],
            "015006": [-61.09525, -1.050202, -60.12882, -0.06843676],
            "015007": [-61.11932, -2.006346, -60.14955, -1.02753],
            "015008": [-61.14355, -2.959792, -60.17041, -1.983664],
            "015009": [-61.16795, -3.91079, -60.19141, -2.937094],
            "015010": [-61.19252, -4.859591, -60.21256, -3.888071],
            "015011": [-61.21725, -5.80644, -60.23386, -4.836844],
            "015012": [-61.24216, -6.75158, -60.2553, -5.783658],
            "015013": [-61.26724, -7.695253, -60.27689, -6.728758],
            "015014": [-61.29249, -8.637702, -60.29863, -7.672385],
            "015015": [-61.31792, -9.579166, -60.32052, -8.614781],
            "015016": [-61.34353, -10.51989, -60.34256, -9.556187],
            "015017": [-61.36931, -11.46011, -60.36475, -10.49684],
            "015018": [-61.39528, -12.40008, -60.38711, -11.43699],
            "015019": [-61.42143, -13.34003, -60.40961, -12.37688],
            "015020": [-61.44777, -14.28023, -60.43228, -13.31676],
            "015021": [-61.47429, -15.22092, -60.45511, -14.25686],
            "016000": [-60.02727, 4.738496, -59.08337, 5.740484],
            "016001": [-60.04731, 3.760636, -59.10022, 4.757991],
            "016002": [-60.06748, 2.787085, -59.11718, 3.780107],
            "016003": [-60.08779, 1.817557, -59.13425, 2.806537],
            "016004": [-60.10824, 0.8517739, -59.15144, 1.836996],
            "016005": [-60.12882, -0.1105352, -59.16874, 0.871206],
            "016006": [-60.14955, -1.069636, -59.18616, -0.09110515],
            "016007": [-60.17041, -2.025788, -59.2037, -1.050202],
            "016008": [-60.19141, -2.979247, -59.22136, -2.006346],
            "016009": [-60.21256, -3.930265, -59.23913, -2.959792],
            "016010": [-60.23386, -4.87909, -59.25703, -3.91079],
            "016011": [-60.2553, -5.825968, -59.27505, -4.859591],
            "016012": [-60.27689, -6.771142, -59.2932, -5.80644],
            "016013": [-60.29863, -7.714855, -59.31147, -6.75158],
            "016014": [-60.32052, -8.657349, -59.32986, -7.695253],
            "016015": [-60.34256, -9.598864, -59.34839, -8.637702],
            "016016": [-60.36475, -10.53964, -59.36704, -9.579166],
            "016017": [-60.38711, -11.47993, -59.38582, -10.51989],
            "016018": [-60.40961, -12.41996, -59.40474, -11.46011],
            "016019": [-60.43228, -13.35999, -59.42379, -12.40008],
            "016020": [-60.45511, -14.30026, -59.44297, -13.34003],
            "016021": [-60.47811, -15.24104, -59.46229, -14.28023],
            "016022": [-60.50126, -16.18256, -59.48175, -15.22092],
            "016023": [-60.52458, -17.1251, -59.50135, -16.16235],
            "017004": [-59.16874, 0.8355805, -58.21497, 1.817557],
            "017005": [-59.18616, -0.1267269, -58.22913, 0.8517739],
            "017006": [-59.2037, -1.08583, -58.24338, -0.1105352],
            "017007": [-59.22136, -2.041989, -58.25773, -1.069636],
            "017008": [-59.23913, -2.99546, -58.27218, -2.025788],
            "017009": [-59.25703, -3.946494, -58.28673, -2.979247],
            "017010": [-59.27505, -4.895339, -58.30137, -3.930265],
            "017011": [-59.2932, -5.842241, -58.31612, -4.87909],
            "017012": [-59.31147, -6.787445, -58.33097, -5.825968],
            "017013": [-59.32986, -7.731191, -58.34592, -6.771142],
            "017014": [-59.34839, -8.673723, -58.36097, -7.714855],
            "017015": [-59.36704, -9.61528, -58.37613, -8.657349],
            "017016": [-59.38582, -10.55611, -58.39139, -9.598864],
            "017017": [-59.40474, -11.49644, -58.40676, -10.53964],
            "017018": [-59.42379, -12.43653, -58.42224, -11.47993],
            "017019": [-59.44297, -13.37662, -58.43783, -12.41996],
            "017020": [-59.46229, -14.31696, -58.45353, -13.35999],
            "017021": [-59.48175, -15.2578, -58.46934, -14.30026],
            "017022": [-59.50135, -16.1994, -58.48526, -15.24104],
            "017023": [-59.52109, -17.14203, -58.5013, -16.18256],
            "018003": [-58.21497, 1.788398, -57.26747, 2.770875],
            "018004": [-58.22913, 0.8226258, -57.27841, 1.801357],
            "018005": [-58.24338, -0.1396803, -57.28942, 0.8355805],
            "018006": [-58.25773, -1.098786, -57.30051, -0.1267269],
            "018007": [-58.27218, -2.054951, -57.31167, -1.08583],
            "018008": [-58.28673, -3.008431, -57.32291, -2.041989],
            "018009": [-58.30137, -3.959477, -57.33423, -2.99546],
            "018010": [-58.31612, -4.908339, -57.34562, -3.946494],
            "018011": [-58.33097, -5.855261, -57.35709, -4.895339],
            "018012": [-58.34592, -6.800487, -57.36864, -5.842241],
            "018013": [-58.36097, -7.74426, -57.38027, -6.787445],
            "018014": [-58.37613, -8.686822, -57.39197, -7.731191],
            "018015": [-58.39139, -9.628413, -57.40377, -8.673723],
            "018016": [-58.40676, -10.56928, -57.41564, -9.61528],
            "018017": [-58.42224, -11.50965, -57.42759, -10.55611],
            "018018": [-58.43783, -12.44979, -57.43963, -11.49644],
            "018019": [-58.45353, -13.38993, -57.45176, -12.43653],
            "018020": [-58.46934, -14.33032, -57.46397, -13.37662],
            "018021": [-58.48526, -15.27122, -57.47627, -14.31696],
            "018022": [-58.5013, -16.21288, -57.48865, -15.2578],
            "018023": [-58.51745, -17.15557, -57.50113, -16.1994],
            "018024": [-58.53371, -18.09954, -57.51369, -17.14203],
            "018025": [-58.5501, -19.04509, -57.52634, -18.08594],
            "018026": [-58.5666, -19.99248, -57.53909, -19.03141],
            "018027": [-58.58323, -20.94201, -57.55193, -19.97872],
            "018028": [-58.59997, -21.89397, -57.56486, -20.92816],
            "018029": [-58.61684, -22.84868, -57.57788, -21.88004],
            "019003": [-57.27841, 1.778678, -56.33396, 2.757907],
            "019004": [-57.28942, 0.8129098, -56.34177, 1.788398],
            "019005": [-57.30051, -0.1493954, -56.34964, 0.8226258],
            "019006": [-57.31167, -1.108503, -56.35756, -0.1396803],
            "019007": [-57.32291, -2.064672, -56.36553, -1.098786],
            "019008": [-57.33423, -3.018159, -56.37356, -2.054951],
            "019009": [-57.34562, -3.969215, -56.38164, -3.008431],
            "019010": [-57.35709, -4.918089, -56.38978, -3.959477],
            "019011": [-57.36864, -5.865025, -56.39798, -4.908339],
            "019012": [-57.38027, -6.810269, -56.40623, -5.855261],
            "019013": [-57.39197, -7.754063, -56.41453, -6.800487],
            "019014": [-57.40377, -8.696647, -56.4229, -7.74426],
            "019015": [-57.41564, -9.638264, -56.43132, -8.686822],
            "019016": [-57.42759, -10.57915, -56.4398, -9.628413],
            "019017": [-57.43963, -11.51956, -56.44834, -10.56928],
            "019018": [-57.45176, -12.45973, -56.45694, -11.50965],
            "019019": [-57.46397, -13.39991, -56.4656, -12.44979],
            "019020": [-57.47627, -14.34034, -56.47433, -13.38993],
            "019021": [-57.48865, -15.28128, -56.48311, -14.33032],
            "019022": [-57.50113, -16.22299, -56.49196, -15.27122],
            "019023": [-57.51369, -17.16572, -56.50087, -16.21288],
            "019024": [-57.52634, -18.10975, -56.50984, -17.15557],
            "019025": [-57.53909, -19.05535, -56.51888, -18.09954],
            "019026": [-57.55193, -20.0028, -56.52799, -19.04509],
            "019027": [-57.56486, -20.95239, -56.53716, -19.99248],
            "019028": [-57.57788, -21.90443, -56.54639, -20.94201],
            "019029": [-57.591, -22.8592, -56.5557, -21.89397],
            "019036": [-57.68562, -29.64835, -56.62279, -28.65349],
            "019037": [-57.69954, -30.63793, -56.63266, -29.63721],
            "020003": [-56.34177, 1.772198, -55.4004, 2.748181],
            "020004": [-56.34964, 0.8064325, -55.40508, 1.778678],
            "020005": [-56.35756, -0.1558721, -55.40981, 0.8129098],
            "020006": [-56.36553, -1.11498, -55.41456, -0.1493954],
            "020007": [-56.37356, -2.071153, -55.41934, -1.108503],
            "020008": [-56.38164, -3.024644, -55.42416, -2.064672],
            "020009": [-56.38978, -3.975707, -55.42901, -3.018159],
            "020010": [-56.39798, -4.924589, -55.43389, -3.969215],
            "020011": [-56.40623, -5.871535, -55.43881, -4.918089],
            "020012": [-56.41453, -6.816791, -55.44376, -5.865025],
            "020013": [-56.4229, -7.760598, -55.44874, -6.810269],
            "020014": [-56.43132, -8.703197, -55.45376, -7.754063],
            "020015": [-56.4398, -9.644831, -55.45882, -8.696647],
            "020016": [-56.44834, -10.58574, -55.4639, -9.638264],
            "020017": [-56.45694, -11.52617, -55.46903, -10.57915],
            "020018": [-56.4656, -12.46636, -55.47419, -11.51956],
            "020019": [-56.47433, -13.40656, -55.47939, -12.45973],
            "020020": [-56.48311, -14.34702, -55.48462, -13.39991],
            "020021": [-56.49196, -15.28799, -55.48989, -14.34034],
            "020022": [-56.50087, -16.22972, -55.4952, -15.28128],
            "020023": [-56.50984, -17.17249, -55.50055, -16.22299],
            "020024": [-56.51888, -18.11656, -55.50593, -17.16572],
            "020025": [-56.52799, -19.0622, -55.51136, -18.10975],
            "020026": [-56.53716, -20.00969, -55.51682, -19.05535],
            "020027": [-56.54639, -20.95932, -55.52232, -20.0028],
            "020028": [-56.5557, -21.91139, -55.52786, -20.95239],
            "020029": [-56.56507, -22.86622, -55.53345, -21.90443],
            "020030": [-56.57451, -23.82412, -55.53907, -22.8592],
            "020035": [-56.62279, -28.67189, -55.56782, -27.68602],
            "020036": [-56.63266, -29.65578, -55.5737, -28.66453],
            "020037": [-56.64261, -30.64543, -55.57962, -29.64835],
            "020038": [-56.65263, -31.64133, -55.58559, -30.63793],
            "021003": [-55.40508, 1.768959, -54.4668, 2.741697],
            "021004": [-55.40981, 0.8031939, -54.46837, 1.772198],
            "021005": [-55.41456, -0.1591104, -54.46994, 0.8064325],
            "021006": [-55.41934, -1.118219, -54.47152, -0.1558721],
            "021007": [-55.42416, -2.074393, -54.47312, -1.11498],
            "021008": [-55.42901, -3.027887, -54.47472, -2.071153],
            "021009": [-55.43389, -3.978953, -54.47634, -3.024644],
            "021010": [-55.43881, -4.927839, -54.47797, -3.975707],
            "021011": [-55.44376, -5.87479, -54.47961, -4.924589],
            "021012": [-55.44874, -6.820052, -54.48126, -5.871535],
            "021013": [-55.45376, -7.763865, -54.48292, -6.816791],
            "021014": [-55.45882, -8.706472, -54.48459, -7.760598],
            "021015": [-55.4639, -9.648114, -54.48628, -8.703197],
            "021016": [-55.46903, -10.58903, -54.48797, -9.644831],
            "021017": [-55.47419, -11.52947, -54.48968, -10.58574],
            "021018": [-55.47939, -12.46967, -54.4914, -11.52617],
            "021019": [-55.48462, -13.40989, -54.49313, -12.46636],
            "021020": [-55.48989, -14.35036, -54.49488, -13.40656],
            "021021": [-55.4952, -15.29134, -54.49663, -14.34702],
            "021022": [-55.50055, -16.23309, -54.4984, -15.28799],
            "021023": [-55.50593, -17.17588, -54.50019, -16.22972],
            "021024": [-55.51136, -18.11996, -54.50198, -17.17249],
            "021025": [-55.51682, -19.06562, -54.50379, -18.11656],
            "021026": [-55.52232, -20.01313, -54.50561, -19.0622],
            "021027": [-55.52786, -20.96278, -54.50744, -20.00969],
            "021028": [-55.53345, -21.91488, -54.50929, -20.95932],
            "021029": [-55.53907, -22.86973, -54.51115, -21.91139],
            "021030": [-55.54474, -23.82765, -54.51303, -22.86622],
            "021031": [-55.55044, -24.78898, -54.51492, -23.82412],
            "021032": [-55.55619, -25.75406, -54.51682, -24.78542],
            "021034": [-55.56782, -27.69696, -54.52067, -26.71965],
            "021035": [-55.5737, -28.67557, -54.52261, -27.69332],
            "021036": [-55.57962, -29.65949, -54.52457, -28.67189],
            "021037": [-55.58559, -30.64919, -54.52655, -29.65578],
            "021038": [-55.59161, -31.64512, -54.52854, -30.64543],
            "022003": [-54.46837, 1.768959, -53.53163, 2.738455],
            "022004": [-54.46994, 0.8031939, -53.53006, 1.768959],
            "022005": [-54.47152, -0.1591104, -53.52848, 0.8031939],
            "022006": [-54.47312, -1.118219, -53.52688, -0.1591104],
            "022007": [-54.47472, -2.074393, -53.52528, -1.118219],
            "022008": [-54.47634, -3.027887, -53.52366, -2.074393],
            "022009": [-54.47797, -3.978953, -53.52203, -3.027887],
            "022010": [-54.47961, -4.927839, -53.52039, -3.978953],
            "022011": [-54.48126, -5.87479, -53.51874, -4.927839],
            "022012": [-54.48292, -6.820052, -53.51708, -5.87479],
            "022013": [-54.48459, -7.763865, -53.51541, -6.820052],
            "022014": [-54.48628, -8.706472, -53.51372, -7.763865],
            "022015": [-54.48797, -9.648114, -53.51203, -8.706472],
            "022016": [-54.48968, -10.58903, -53.51032, -9.648114],
            "022017": [-54.4914, -11.52947, -53.5086, -10.58903],
            "022018": [-54.49313, -12.46967, -53.50687, -11.52947],
            "022019": [-54.49488, -13.40989, -53.50512, -12.46967],
            "022020": [-54.49663, -14.35036, -53.50337, -13.40989],
            "022021": [-54.4984, -15.29134, -53.5016, -14.35036],
            "022022": [-54.50019, -16.23309, -53.49981, -15.29134],
            "022023": [-54.50198, -17.17588, -53.49802, -16.23309],
            "022024": [-54.50379, -18.11996, -53.49621, -17.17588],
            "022025": [-54.50561, -19.06562, -53.49439, -18.11996],
            "022026": [-54.50744, -20.01313, -53.49256, -19.06562],
            "022027": [-54.50929, -20.96278, -53.49071, -20.01313],
            "022028": [-54.51115, -21.91488, -53.48885, -20.96278],
            "022029": [-54.51303, -22.86973, -53.48697, -21.91488],
            "022030": [-54.51492, -23.82765, -53.48508, -22.86973],
            "022031": [-54.51682, -24.78898, -53.48318, -23.82765],
            "022032": [-54.51874, -25.75406, -53.48126, -24.78898],
            "022033": [-54.52067, -26.72326, -53.47933, -25.75406],
            "022034": [-54.52261, -27.69696, -53.47739, -26.72326],
            "022035": [-54.52457, -28.67557, -53.47543, -27.69696],
            "022036": [-54.52655, -29.65949, -53.47345, -28.67557],
            "022037": [-54.52854, -30.64919, -53.47146, -29.65949],
            "022038": [-54.53054, -31.64512, -53.46946, -30.64919],
            "022039": [-54.53256, -32.6478, -53.46744, -31.64512],
            "022040": [-54.5346, -33.65776, -53.4654, -32.6478],
            "022041": [-54.53665, -34.67556, -53.46335, -33.65776],
            "023003": [-53.5332, 1.768959, -52.59492, 2.741697],
            "023004": [-53.53163, 0.8031939, -52.59019, 1.772198],
            "023005": [-53.53006, -0.1591104, -52.58544, 0.8064325],
            "023006": [-53.52848, -1.118219, -52.58066, -0.1558721],
            "023007": [-53.52688, -2.074393, -52.57584, -1.11498],
            "023008": [-53.52528, -3.027887, -52.57099, -2.071153],
            "023009": [-53.52366, -3.978953, -52.56611, -3.024644],
            "023010": [-53.52203, -4.927839, -52.56119, -3.975707],
            "023011": [-53.52039, -5.87479, -52.55624, -4.924589],
            "023012": [-53.51874, -6.820052, -52.55126, -5.871535],
            "023013": [-53.51708, -7.763865, -52.54624, -6.816791],
            "023014": [-53.51541, -8.706472, -52.54118, -7.760598],
            "023015": [-53.51372, -9.648114, -52.5361, -8.703197],
            "023016": [-53.51203, -10.58903, -52.53097, -9.644831],
            "023017": [-53.51032, -11.52947, -52.52581, -10.58574],
            "023018": [-53.5086, -12.46967, -52.52061, -11.52617],
            "023019": [-53.50687, -13.40989, -52.51538, -12.46636],
            "023020": [-53.50512, -14.35036, -52.51011, -13.40656],
            "023021": [-53.50337, -15.29134, -52.5048, -14.34702],
            "023022": [-53.5016, -16.23309, -52.49945, -15.28799],
            "023023": [-53.49981, -17.17588, -52.49407, -16.22972],
            "023024": [-53.49802, -18.11996, -52.48864, -17.17249],
            "023025": [-53.49621, -19.06562, -52.48318, -18.11656],
            "023026": [-53.49439, -20.01313, -52.47768, -19.0622],
            "023027": [-53.49256, -20.96278, -52.47214, -20.00969],
            "023028": [-53.49071, -21.91488, -52.46655, -20.95932],
            "023029": [-53.48885, -22.86973, -52.46093, -21.91139],
            "023030": [-53.48697, -23.82765, -52.45526, -22.86622],
            "023031": [-53.48508, -24.78898, -52.44956, -23.82412],
            "023032": [-53.48318, -25.75406, -52.44381, -24.78542],
            "023033": [-53.48126, -26.72326, -52.43801, -25.75047],
            "023034": [-53.47933, -27.69696, -52.43218, -26.71965],
            "023035": [-53.47739, -28.67557, -52.4263, -27.69332],
            "023036": [-53.47543, -29.65949, -52.42038, -28.67189],
            "023037": [-53.47345, -30.64919, -52.41441, -29.65578],
            "023038": [-53.47146, -31.64512, -52.40839, -30.64543],
            "023039": [-53.46946, -32.6478, -52.40233, -31.64133],
            "023040": [-53.46744, -33.65776, -52.39623, -32.64397],
            "023041": [-53.4654, -34.67556, -52.39007, -33.65388],
            "024001": [-52.60889, 3.715205, -51.6738, 4.699508],
            "024002": [-52.60426, 2.741697, -51.66604, 3.721695],
            "024003": [-52.5996, 1.772198, -51.65823, 2.748181],
            "024004": [-52.59492, 0.8064325, -51.65036, 1.778678],
            "024005": [-52.59019, -0.1558721, -51.64244, 0.8129098],
            "024006": [-52.58544, -1.11498, -51.63447, -0.1493954],
            "024007": [-52.58066, -2.071153, -51.62644, -1.108503],
            "024008": [-52.57584, -3.024644, -51.61836, -2.064672],
            "024009": [-52.57099, -3.975707, -51.61022, -3.018159],
            "024010": [-52.56611, -4.924589, -51.60202, -3.969215],
            "024011": [-52.56119, -5.871535, -51.59377, -4.918089],
            "024012": [-52.55624, -6.816791, -51.58547, -5.865025],
            "024013": [-52.55126, -7.760598, -51.5771, -6.810269],
            "024014": [-52.54624, -8.703197, -51.56868, -7.754063],
            "024015": [-52.54118, -9.644831, -51.5602, -8.696647],
            "024016": [-52.5361, -10.58574, -51.55166, -9.638264],
            "024017": [-52.53097, -11.52617, -51.54306, -10.57915],
            "024018": [-52.52581, -12.46636, -51.5344, -11.51956],
            "024019": [-52.52061, -13.40656, -51.52567, -12.45973],
            "024020": [-52.51538, -14.34702, -51.51689, -13.39991],
            "024021": [-52.51011, -15.28799, -51.50804, -14.34034],
            "024022": [-52.5048, -16.22972, -51.49913, -15.28128],
            "024023": [-52.49945, -17.17249, -51.49016, -16.22299],
            "024024": [-52.49407, -18.11656, -51.48112, -17.16572],
            "024025": [-52.48864, -19.0622, -51.47201, -18.10975],
            "024026": [-52.48318, -20.00969, -51.46284, -19.05535],
            "024027": [-52.47768, -20.95932, -51.45361, -20.0028],
            "024028": [-52.47214, -21.91139, -51.4443, -20.95239],
            "024029": [-52.46655, -22.86622, -51.43493, -21.90443],
            "024030": [-52.46093, -23.82412, -51.42549, -22.8592],
            "024031": [-52.45526, -24.78542, -51.41597, -23.81705],
            "024032": [-52.44956, -25.75047, -51.40639, -24.7783],
            "024033": [-52.44381, -26.71965, -51.39674, -25.7433],
            "024034": [-52.43801, -27.69332, -51.38701, -26.71242],
            "024035": [-52.43218, -28.67189, -51.37721, -27.68602],
            "024036": [-52.4263, -29.65578, -51.36734, -28.66453],
            "024037": [-52.42038, -30.64543, -51.35739, -29.64835],
            "024038": [-52.41441, -31.64133, -51.34737, -30.63793],
            "024039": [-52.40839, -32.64397, -51.33727, -31.63375],
            "025001": [-51.68152, 3.721695, -50.7434, 4.709255],
            "025002": [-51.6738, 2.748181, -50.73253, 3.73143],
            "025003": [-51.66604, 1.778678, -50.72159, 2.757907],
            "025004": [-51.65823, 0.8129098, -50.71058, 1.788398],
            "025005": [-51.65036, -0.1493954, -50.69949, 0.8226258],
            "025006": [-51.64244, -1.108503, -50.68833, -0.1396803],
            "025007": [-51.63447, -2.064672, -50.67709, -1.098786],
            "025008": [-51.62644, -3.018159, -50.66577, -2.054951],
            "025009": [-51.61836, -3.969215, -50.65438, -3.008431],
            "025010": [-51.61022, -4.918089, -50.64291, -3.959477],
            "025011": [-51.60202, -5.865025, -50.63136, -4.908339],
            "025012": [-51.59377, -6.810269, -50.61973, -5.855261],
            "025013": [-51.58547, -7.754063, -50.60803, -6.800487],
            "025014": [-51.5771, -8.696647, -50.59623, -7.74426],
            "025015": [-51.56868, -9.638264, -50.58436, -8.686822],
            "025016": [-51.5602, -10.57915, -50.57241, -9.628413],
            "025017": [-51.55166, -11.51956, -50.56037, -10.56928],
            "025018": [-51.54306, -12.45973, -50.54824, -11.50965],
            "025019": [-51.5344, -13.39991, -50.53603, -12.44979],
            "025020": [-51.52567, -14.34034, -50.52373, -13.38993],
            "025021": [-51.51689, -15.28128, -50.51135, -14.33032],
            "025022": [-51.50804, -16.22299, -50.49887, -15.27122],
            "025023": [-51.49913, -17.16572, -50.48631, -16.21288],
            "025024": [-51.49016, -18.10975, -50.47366, -17.15557],
            "025025": [-51.48112, -19.05535, -50.46091, -18.09954],
            "025026": [-51.47201, -20.0028, -50.44807, -19.04509],
            "025027": [-51.46284, -20.95239, -50.43514, -19.99248],
            "025028": [-51.45361, -21.90443, -50.42212, -20.94201],
            "025029": [-51.4443, -22.8592, -50.409, -21.89397],
            "025030": [-51.43493, -23.81705, -50.39578, -22.84868],
            "025031": [-51.42549, -24.7783, -50.38246, -23.80646],
            "025032": [-51.41597, -25.7433, -50.36905, -24.76763],
            "025033": [-51.40639, -26.71242, -50.35553, -25.73254],
            "025034": [-51.39674, -27.68602, -50.34192, -26.70157],
            "025035": [-51.38701, -28.66453, -50.3282, -27.67509],
            "025036": [-51.37721, -29.64835, -50.31438, -28.65349],
            "025037": [-51.36734, -30.63793, -50.30046, -29.63721],
            "025038": [-51.35739, -31.63375, -50.28643, -30.62668],
            "025039": [-51.34737, -32.63631, -50.27229, -31.62238],
            "026003": [-50.73253, 1.788398, -49.78503, 2.770875],
            "026004": [-50.72159, 0.8226258, -49.77087, 1.801357],
            "026005": [-50.71058, -0.1396803, -49.75662, 0.8355805],
            "026006": [-50.69949, -1.098786, -49.74227, -0.1267269],
            "026007": [-50.68833, -2.054951, -49.72782, -1.08583],
            "026008": [-50.67709, -3.008431, -49.71327, -2.041989],
            "026009": [-50.66577, -3.959477, -49.69863, -2.99546],
            "026010": [-50.65438, -4.908339, -49.68388, -3.946494],
            "026011": [-50.64291, -5.855261, -49.66903, -4.895339],
            "026012": [-50.63136, -6.800487, -49.65408, -5.842241],
            "026013": [-50.61973, -7.74426, -49.63903, -6.787445],
            "026014": [-50.60803, -8.686822, -49.62387, -7.731191],
            "026015": [-50.59623, -9.628413, -49.60861, -8.673723],
            "026016": [-50.58436, -10.56928, -49.59324, -9.61528],
            "026017": [-50.57241, -11.50965, -49.57776, -10.55611],
            "026018": [-50.56037, -12.44979, -49.56217, -11.49644],
            "026019": [-50.54824, -13.38993, -49.54647, -12.43653],
            "026020": [-50.53603, -14.33032, -49.53066, -13.37662],
            "026021": [-50.52373, -15.27122, -49.51474, -14.31696],
            "026022": [-50.51135, -16.21288, -49.4987, -15.2578],
            "026023": [-50.49887, -17.15557, -49.48255, -16.1994],
            "026024": [-50.48631, -18.09954, -49.46629, -17.14203],
            "026025": [-50.47366, -19.04509, -49.4499, -18.08594],
            "026026": [-50.46091, -19.99248, -49.4334, -19.03141],
            "026027": [-50.44807, -20.94201, -49.41677, -19.97872],
            "026028": [-50.43514, -21.89397, -49.40003, -20.92816],
            "026029": [-50.42212, -22.84868, -49.38316, -21.88004],
            "026030": [-50.409, -23.80646, -49.36617, -22.83466],
            "026031": [-50.39578, -24.76763, -49.34905, -23.79233],
            "026032": [-50.38246, -25.73254, -49.33181, -24.7534],
            "026033": [-50.36905, -26.70157, -49.31443, -25.7182],
            "026034": [-50.35553, -27.67509, -49.29693, -26.68711],
            "026035": [-50.34192, -28.65349, -49.2793, -27.6605],
            "026036": [-50.3282, -29.63721, -49.26153, -28.63878],
            "026037": [-50.31438, -30.62668, -49.24363, -29.62235],
            "027005": [-49.77087, -0.1267269, -48.81384, 0.8517739],
            "027006": [-49.75662, -1.08583, -48.7963, -0.1105352],
            "027007": [-49.74227, -2.041989, -48.77864, -1.069636],
            "027008": [-49.72782, -2.99546, -48.76087, -2.025788],
            "027009": [-49.71327, -3.946494, -48.74297, -2.979247],
            "027010": [-49.69863, -4.895339, -48.72495, -3.930265],
            "027011": [-49.68388, -5.842241, -48.7068, -4.87909],
            "027012": [-49.66903, -6.787445, -48.68853, -5.825968],
            "027013": [-49.65408, -7.731191, -48.67014, -6.771142],
            "027014": [-49.63903, -8.673723, -48.65161, -7.714855],
            "027015": [-49.62387, -9.61528, -48.63296, -8.657349],
            "027016": [-49.60861, -10.55611, -48.61418, -9.598864],
            "027017": [-49.59324, -11.49644, -48.59526, -10.53964],
            "027018": [-49.57776, -12.43653, -48.57621, -11.47993],
            "027019": [-49.56217, -13.37662, -48.55703, -12.41996],
            "027020": [-49.54647, -14.31696, -48.53771, -13.35999],
            "027021": [-49.53066, -15.2578, -48.51825, -14.30026],
            "027022": [-49.51474, -16.1994, -48.49865, -15.24104],
            "027023": [-49.4987, -17.14203, -48.47891, -16.18256],
            "027024": [-49.48255, -18.08594, -48.45903, -17.1251],
            "027025": [-49.46629, -19.03141, -48.43901, -18.06892],
            "027026": [-49.4499, -19.97872, -48.41884, -19.0143],
            "027027": [-49.4334, -20.92816, -48.39853, -19.96152],
            "027028": [-49.41677, -21.88004, -48.37806, -20.91086],
            "027029": [-49.40003, -22.83466, -48.35745, -21.86263],
            "027030": [-49.38316, -23.79233, -48.33668, -22.81712],
            "027031": [-49.36617, -24.7534, -48.31576, -23.77467],
            "027032": [-49.34905, -25.7182, -48.29469, -24.73561],
            "027033": [-49.33181, -26.68711, -48.27346, -25.70028],
            "027034": [-49.31443, -27.6605, -48.25207, -26.66904],
            "027035": [-49.29693, -28.63878, -48.23052, -27.64228],
            "027036": [-49.2793, -29.62235, -48.20881, -28.62039],
            "028006": [-48.81384, -1.069636, -47.85045, -0.09110515],
            "028007": [-48.7963, -2.025788, -47.82959, -1.050202],
            "028008": [-48.77864, -2.979247, -47.80859, -2.006346],
            "028009": [-48.76087, -3.930265, -47.78744, -2.959792],
            "028010": [-48.74297, -4.87909, -47.76614, -3.91079],
            "028011": [-48.72495, -5.825968, -47.7447, -4.859591],
            "028012": [-48.7068, -6.771142, -47.72311, -5.80644],
            "028013": [-48.68853, -7.714855, -47.70137, -6.75158],
            "028014": [-48.67014, -8.657349, -47.67948, -7.695253],
            "028015": [-48.65161, -9.598864, -47.65744, -8.637702],
            "028016": [-48.63296, -10.53964, -47.63525, -9.579166],
            "028017": [-48.61418, -11.47993, -47.61289, -10.51989],
            "028018": [-48.59526, -12.41996, -47.59039, -11.46011],
            "028019": [-48.57621, -13.35999, -47.56772, -12.40008],
            "028020": [-48.55703, -14.30026, -47.54489, -13.34003],
            "028021": [-48.53771, -15.24104, -47.52189, -14.28023],
            "028022": [-48.51825, -16.18256, -47.49874, -15.22092],
            "028023": [-48.49865, -17.1251, -47.47542, -16.16235],
            "028024": [-48.47891, -18.06892, -47.45193, -17.10479],
            "028025": [-48.45903, -19.0143, -47.42826, -18.04851],
            "028026": [-48.43901, -19.96152, -47.40443, -18.99378],
            "028027": [-48.41884, -20.91086, -47.38043, -19.94088],
            "028028": [-48.39853, -21.86263, -47.35625, -20.8901],
            "028029": [-48.37806, -22.81712, -47.33189, -21.84173],
            "028030": [-48.35745, -23.77467, -47.30735, -22.79609],
            "028031": [-48.33668, -24.73561, -47.28263, -23.75349],
            "028032": [-48.31576, -25.70028, -47.25773, -24.71427],
            "029006": [-47.87118, -1.050202, -46.90475, -0.06843676],
            "029007": [-47.85045, -2.006346, -46.88068, -1.02753],
            "029008": [-47.82959, -2.959792, -46.85645, -1.983664],
            "029009": [-47.80859, -3.91079, -46.83205, -2.937094],
            "029010": [-47.78744, -4.859591, -46.80748, -3.888071],
            "029011": [-47.76614, -5.80644, -46.78275, -4.836844],
            "029012": [-47.7447, -6.75158, -46.75784, -5.783658],
            "029013": [-47.72311, -7.695253, -46.73276, -6.728758],
            "029014": [-47.70137, -8.637702, -46.70751, -7.672385],
            "029015": [-47.67948, -9.579166, -46.68208, -8.614781],
            "029016": [-47.65744, -10.51989, -46.65647, -9.556187],
            "029017": [-47.63525, -11.46011, -46.63069, -10.49684],
            "029018": [-47.61289, -12.40008, -46.60472, -11.43699],
            "029019": [-47.59039, -13.34003, -46.57857, -12.37688],
            "029020": [-47.56772, -14.28023, -46.55223, -13.31676],
            "029021": [-47.54489, -15.22092, -46.52571, -14.25686],
            "029022": [-47.52189, -16.16235, -46.49899, -15.19745],
            "029023": [-47.49874, -17.10479, -46.47209, -16.13878],
            "029024": [-47.47542, -18.04851, -46.44499, -17.08111],
            "029025": [-47.45193, -18.99378, -46.41769, -18.0247],
            "029026": [-47.42826, -19.94088, -46.3902, -18.96984],
            "029027": [-47.40443, -20.8901, -46.3625, -19.91681],
            "029028": [-47.38043, -21.84173, -46.33461, -20.86588],
            "029029": [-47.35625, -22.79609, -46.30651, -21.81736],
            "029030": [-47.33189, -23.75349, -46.2782, -22.77155],
            "029031": [-47.30735, -24.71427, -46.24969, -23.72878],
            "030006": [-46.92866, -1.02753, -45.95921, -0.04253005],
            "030007": [-46.90475, -1.983664, -45.93193, -1.00162],
            "030008": [-46.88068, -2.937094, -45.90447, -1.957743],
            "030009": [-46.85645, -3.888071, -45.87683, -2.911154],
            "030010": [-46.83205, -4.836844, -45.84899, -3.862106],
            "030011": [-46.80748, -5.783658, -45.82096, -4.810847],
            "030012": [-46.78275, -6.728758, -45.79274, -5.757623],
            "030013": [-46.75784, -7.672385, -45.76432, -6.702677],
            "030014": [-46.73276, -8.614781, -45.73571, -7.646251],
            "030015": [-46.70751, -9.556187, -45.7069, -8.588587],
            "030016": [-46.68208, -10.49684, -45.67788, -9.529926],
            "030017": [-46.65647, -11.43699, -45.64866, -10.47051],
            "030018": [-46.63069, -12.37688, -45.61924, -11.41058],
            "030019": [-46.60472, -13.31676, -45.5896, -12.35038],
            "030020": [-46.57857, -14.25686, -45.55976, -13.29015],
            "030021": [-46.55223, -15.19745, -45.52971, -14.23015],
            "030022": [-46.52571, -16.13878, -45.49944, -15.17063],
            "030023": [-46.49899, -17.08111, -45.46895, -16.11184],
            "030024": [-46.47209, -18.0247, -45.43824, -17.05404],
            "030025": [-46.44499, -18.96984, -45.40731, -17.9975],
            "030026": [-46.41769, -19.91681, -45.37616, -18.94249],
            "030027": [-46.3902, -20.86588, -45.34478, -19.8893],
            "030028": [-46.3625, -21.81736, -45.31317, -20.8382],
            "030029": [-46.33461, -22.77155, -45.28133, -21.7895],
            "030030": [-46.30651, -23.72878, -45.24926, -22.74351],
            "030031": [-46.2782, -24.68937, -45.21695, -23.70054],
            "031007": [-45.95921, -1.957743, -44.98337, -0.9724706],
            "031008": [-45.93193, -2.911154, -44.95269, -1.928581],
            "031009": [-45.90447, -3.862106, -44.92179, -2.881973],
            "031010": [-45.87683, -4.810847, -44.89069, -3.832897],
            "031011": [-45.84899, -5.757623, -44.85937, -4.781603],
            "031012": [-45.82096, -6.702677, -44.82784, -5.728335],
            "031013": [-45.79274, -7.646251, -44.79608, -6.673337],
            "031014": [-45.76432, -8.588587, -44.76411, -7.616852],
            "031015": [-45.73571, -9.529926, -44.73191, -8.559121],
            "031016": [-45.7069, -10.47051, -44.69949, -9.500385],
            "031017": [-45.67788, -11.41058, -44.66684, -10.44088],
            "031018": [-45.64866, -12.35038, -44.63396, -11.38086],
            "031019": [-45.61924, -13.29015, -44.60085, -12.32056],
            "031020": [-45.5896, -14.23015, -44.56751, -13.26023],
            "031021": [-45.55976, -15.17063, -44.53392, -14.20011],
            "031022": [-45.52971, -16.11184, -44.5001, -15.14046],
            "031023": [-45.49944, -17.05404, -44.46603, -16.08153],
            "031024": [-45.46895, -17.9975, -44.43172, -17.02359],
            "031025": [-45.43824, -18.94249, -44.39716, -17.9669],
            "031026": [-45.40731, -19.8893, -44.36235, -18.91172],
            "031027": [-45.37616, -20.8382, -44.32729, -19.85835],
            "031028": [-45.34478, -21.7895, -44.29197, -20.80707],
            "031029": [-45.31317, -22.74351, -44.25639, -21.75818],
            "031030": [-45.28133, -23.70054, -44.22056, -22.71198],
            "031031": [-45.24926, -24.66093, -44.18445, -23.66879],
            "032007": [-45.01385, -1.928581, -44.03502, -0.9400828],
            "032008": [-44.98337, -2.881973, -44.00111, -1.89618],
            "032009": [-44.95269, -3.832897, -43.96697, -2.84955],
            "032010": [-44.92179, -4.781603, -43.9326, -3.800444],
            "032011": [-44.89069, -5.728335, -43.898, -4.74911],
            "032012": [-44.85937, -6.673337, -43.86315, -5.695794],
            "032013": [-44.82784, -7.616852, -43.82806, -6.64074],
            "032014": [-44.79608, -8.559121, -43.79273, -7.58419],
            "032015": [-44.76411, -9.500385, -43.75715, -8.526384],
            "032016": [-44.73191, -10.44088, -43.72133, -9.467565],
            "032017": [-44.69949, -11.38086, -43.68525, -10.40797],
            "032018": [-44.66684, -12.32056, -43.64892, -11.34785],
            "032019": [-44.63396, -13.26023, -43.61233, -12.28744],
            "032020": [-44.60085, -14.20011, -43.57549, -13.22698],
            "032021": [-44.56751, -15.14046, -43.53838, -14.16674],
            "032022": [-44.53392, -16.08153, -43.501, -15.10695],
            "032023": [-44.5001, -17.02359, -43.46336, -16.04787],
            "032024": [-44.46603, -17.9669, -43.42545, -16.98976],
            "032025": [-44.43172, -18.91172, -43.38726, -17.9329],
            "032026": [-44.39716, -19.85835, -43.3488, -18.87754],
            "032027": [-44.36235, -20.80707, -43.31005, -19.82398],
            "032028": [-44.32729, -21.75818, -43.27103, -20.77249],
            "032029": [-44.29197, -22.71198, -43.23172, -21.72338],
            "032030": [-44.25639, -23.66879, -43.19212, -22.67695],
            "033008": [-44.03502, -2.84955, -43.04977, -1.86054],
            "033009": [-44.00111, -3.800444, -43.01239, -2.813886],
            "033010": [-43.96697, -4.74911, -42.97475, -3.764746],
            "033011": [-43.9326, -5.695794, -42.93686, -4.71337],
            "033012": [-43.898, -6.64074, -42.8987, -5.660001],
            "033013": [-43.86315, -7.58419, -42.86029, -6.604885],
            "033014": [-43.82806, -8.526384, -42.8216, -7.548263],
            "033015": [-43.79273, -9.467565, -42.78265, -8.490377],
            "033016": [-43.75715, -10.40797, -42.74342, -9.431466],
            "033017": [-43.72133, -11.34785, -42.70392, -10.37177],
            "033018": [-43.68525, -12.28744, -42.66414, -11.31154],
            "033019": [-43.64892, -13.22698, -42.62407, -12.25101],
            "033020": [-43.61233, -14.16674, -42.58373, -13.19042],
            "033021": [-43.57549, -15.10695, -42.5431, -14.13003],
            "033022": [-43.53838, -16.04787, -42.50217, -15.07009],
            "033023": [-43.501, -16.98976, -42.46096, -16.01084],
            "033024": [-43.46336, -17.9329, -42.41945, -16.95256],
            "033025": [-43.42545, -18.87754, -42.37764, -17.89551],
            "033026": [-43.38726, -19.82398, -42.33552, -18.83995],
            "033027": [-43.3488, -20.77249, -42.2931, -19.78618],
            "033028": [-43.31005, -21.72338, -42.25037, -20.73446],
            "033029": [-43.27103, -22.67695, -42.20733, -21.68511],
            "033030": [-43.23172, -23.63351, -42.16397, -22.63842],
            "034008": [-43.08689, -2.813886, -42.09867, -1.82166],
            "034009": [-43.04977, -3.764746, -42.05806, -2.774981],
            "034010": [-43.01239, -4.71337, -42.01716, -3.725805],
            "034011": [-42.97475, -5.660001, -41.97598, -4.674382],
            "034012": [-42.93686, -6.604885, -41.93452, -5.620957],
            "034013": [-42.8987, -7.548263, -41.89278, -6.565774],
            "034014": [-42.86029, -8.490377, -41.85074, -7.509075],
            "034015": [-42.8216, -9.431466, -41.80841, -8.4511],
            "034016": [-42.78265, -10.37177, -41.76578, -9.39209],
            "034017": [-42.74342, -11.31154, -41.72286, -10.33229],
            "034018": [-42.70392, -12.25101, -41.67963, -11.27193],
            "034019": [-42.66414, -13.19042, -41.6361, -12.21127],
            "034020": [-42.62407, -14.13003, -41.59226, -13.15054],
            "034021": [-42.58373, -15.07009, -41.54811, -14.08999],
            "034022": [-42.5431, -16.01084, -41.50364, -15.02988],
            "034023": [-42.50217, -16.95256, -41.45885, -15.97046],
            "034024": [-42.46096, -17.89551, -41.41375, -16.91199],
            "034025": [-42.41945, -18.83995, -41.36831, -17.85473],
            "034026": [-42.37764, -19.78618, -41.32255, -18.79896],
            "034027": [-42.33552, -20.73446, -41.27646, -19.74495],
            "034028": [-42.2931, -21.68511, -41.23003, -20.69299],
            "034029": [-42.25037, -22.63842, -41.18326, -21.64337],
            "034030": [-42.20733, -23.59472, -41.13614, -22.59641],
            "035008": [-42.13902, -2.774981, -41.14786, -1.779542],
            "035009": [-42.09867, -3.725805, -41.104, -2.732835],
            "035010": [-42.05806, -4.674382, -41.05985, -3.683621],
            "035011": [-42.01716, -5.620957, -41.01539, -4.632148],
            "035012": [-41.97598, -6.565774, -40.97063, -5.578663],
            "035013": [-41.93452, -7.509075, -40.92556, -6.523407],
            "035014": [-41.89278, -8.4511, -40.88017, -7.466624],
            "035015": [-41.85074, -9.39209, -40.83447, -8.408554],
            "035016": [-41.80841, -10.33229, -40.78845, -9.349437],
            "035017": [-41.76578, -11.27193, -40.7421, -10.28952],
            "035018": [-41.72286, -12.21127, -40.69543, -11.22903],
            "035019": [-41.67963, -13.15054, -40.64843, -12.16822],
            "035020": [-41.6361, -14.08999, -40.6011, -13.10734],
            "035021": [-41.59226, -15.02988, -40.55343, -14.04663],
            "035022": [-41.54811, -15.97046, -40.50542, -14.98634],
            "035023": [-41.50364, -16.91199, -40.45707, -15.92672],
            "035024": [-41.45885, -17.85473, -40.40837, -16.86804],
            "035025": [-41.41375, -18.79896, -40.35932, -17.81056],
            "035026": [-41.36831, -19.74495, -40.30991, -18.75455],
            "035027": [-41.32255, -20.69299, -40.26015, -19.70029],
            "035028": [-41.27646, -21.64337, -40.21002, -20.64807],
            "035029": [-41.23003, -22.59641, -40.15953, -21.59817],
            "036009": [-41.14786, -3.683621, -40.15025, -2.687449],
            "036010": [-41.104, -4.632148, -40.10284, -3.638194],
            "036011": [-41.05985, -5.578663, -40.05511, -4.586669],
            "036012": [-41.01539, -6.523407, -40.00704, -5.533118],
            "036013": [-40.97063, -7.466624, -39.95865, -6.477785],
            "036014": [-40.92556, -8.408554, -39.90992, -7.420912],
            "036015": [-40.88017, -9.349437, -39.86085, -8.36274],
            "036016": [-40.83447, -10.28952, -39.81143, -9.30351],
            "036017": [-40.78845, -11.22903, -39.76167, -10.24346],
            "036018": [-40.7421, -12.16822, -39.71156, -11.18284],
            "036019": [-40.69543, -13.10734, -39.6611, -12.12188],
            "036020": [-40.64843, -14.04663, -39.61028, -13.06083],
            "036021": [-40.6011, -14.98634, -39.5591, -13.99993],
            "036022": [-40.55343, -15.92672, -39.50755, -14.93945],
            "036023": [-40.50542, -16.86804, -39.45564, -15.87963],
            "036024": [-40.45707, -17.81056, -39.40335, -16.82073],
            "036025": [-40.40837, -18.75455, -39.35068, -17.76301],
            "036026": [-40.35932, -19.70029, -39.29764, -18.70675],
            "036027": [-40.30991, -20.64807, -39.2442, -19.65222],
            "037009": [-40.19734, -3.638194, -39.19682, -2.638823],
            "037010": [-40.15025, -4.586669, -39.14616, -3.589525],
            "037011": [-40.10284, -5.533118, -39.09515, -4.537943],
            "037012": [-40.05511, -6.477785, -39.04379, -5.484323],
            "037013": [-40.00704, -7.420912, -38.99208, -6.428908],
            "037014": [-39.95865, -8.36274, -38.94, -7.37194],
            "037015": [-39.90992, -9.30351, -38.88757, -8.31366],
            "037016": [-39.86085, -10.24346, -38.83476, -9.254308],
            "037017": [-39.81143, -11.18284, -38.78159, -10.19412],
            "037018": [-39.76167, -12.12188, -38.72805, -11.13335],
            "037019": [-39.71156, -13.06083, -38.67413, -12.07223],
            "037020": [-39.6611, -13.99993, -38.61982, -13.011],
            "037021": [-39.61028, -14.93945, -38.56513, -13.94992],
            "037022": [-39.5591, -15.87963, -38.51005, -14.88923],
            "037023": [-39.50755, -16.82073, -38.45457, -15.82918],
            "037024": [-39.45564, -17.76301, -38.3987, -16.77004],
            "037025": [-39.40335, -18.70675, -38.34243, -17.71207],
            "038009": [-39.24714, -3.589525, -38.24373, -2.586957],
            "038010": [-39.19682, -4.537943, -38.18982, -3.537614],
            "038011": [-39.14616, -5.484323, -38.13555, -4.485973],
            "038012": [-39.09515, -6.428908, -38.08089, -5.43228],
            "038013": [-39.04379, -7.37194, -38.02586, -6.376778],
            "038014": [-38.99208, -8.31366, -37.97045, -7.319709],
            "038015": [-38.94, -9.254308, -37.91465, -8.261314],
            "038016": [-38.88757, -10.19412, -37.85847, -9.201833],
            "038017": [-38.83476, -11.13335, -37.80189, -10.14151],
            "038018": [-38.78159, -12.07223, -37.74491, -11.08057],
            "038019": [-38.72805, -13.011, -37.68753, -12.01928],
            "038020": [-38.67413, -13.94992, -37.62975, -12.95786],
            "039010": [-38.24373, -4.485973, -37.23386, -3.482462],
            "039011": [-38.18982, -5.43228, -37.17631, -4.430759],
            "039012": [-38.13555, -6.376778, -37.11837, -5.376989],
            "039013": [-38.08089, -7.319709, -37.06003, -6.321396],
            "039014": [-38.02586, -8.261314, -37.00128, -7.264221],
            "039015": [-37.97045, -9.201833, -36.94213, -8.205704],
            "039016": [-37.91465, -10.14151, -36.88256, -9.146087],
            "039017": [-37.85847, -11.08057, -36.82258, -10.08561],
            "039018": [-37.80189, -12.01928, -36.76217, -11.02451],
            "039019": [-37.74491, -12.95786, -36.70134, -11.96303],
            "040011": [-37.23386, -5.376989, -36.21748, -4.372301],
            "040012": [-37.17631, -6.321396, -36.15625, -5.318451],
            "040013": [-37.11837, -7.264221, -36.0946, -6.262762],
            "040014": [-37.06003, -8.205704, -36.03253, -7.205475],
            "040015": [-37.00128, -9.146087, -35.97002, -8.146832],
            "040016": [-36.94213, -10.08561, -35.90708, -9.087071],
            "040017": [-36.88256, -11.02451, -35.84369, -10.02643],
            "041011": [-36.27829, -5.318451, -35.25906, -4.3106],
            "041012": [-36.21748, -6.262762, -35.19455, -5.256667],
            "041013": [-36.15625, -7.205475, -35.1296, -6.200877],
            "041014": [-36.0946, -8.146832, -35.0642, -7.143474],
            "041015": [-36.03253, -9.087071, -34.99834, -8.084697],
            "041016": [-35.97002, -10.02643, -34.93203, -9.024786],
            "042012": [-35.25906, -6.200877, -34.2333, -5.191637],
            "042013": [-35.19455, -7.143474, -34.16505, -6.135744],
            "042014": [-35.1296, -8.084697, -34.09633, -7.078219],
            "042015": [-35.0642, -9.024786, -34.02713, -8.019303],
            "043010": [-34.43527, -4.245657, -33.41414, -3.229449],
            "045010": [-32.53366, -4.106048, -31.50707, -3.083508],
            "046028": [-30.12139, -20.95638, -29.02005, -19.92765],
            "047028": [-29.11052, -20.87332, -28.00621, -19.84169],
            "048006": [-30.01128, -0.0656906, -28.99157, 0.9775925],
        }
        
        if tile in known_tiles:
            bbox = known_tiles[tile]
            self.radar_log_output.append(f"BBox do tile {tile}: {bbox}")
            QApplication.processEvents()
            return bbox
        else:
            raise Exception(f"Tile {tile} não encontrado no dicionário de bboxes. Adicione-o manualmente.")

    def build_vh_mosaic(self, target_date, tile):
        """
        Constrói o mosaico temporal VH para uma data alvo e tile.
        Retorna um dicionário com:
          - 'array': numpy array final (média temporal)
          - 'geotransform': geotransform do grid de trabalho (CRS original)
          - 'projection': projeção do grid de trabalho
          - 'dates': lista de strings das datas utilizadas
          - 'scenes': lista de nomes das cenas
          - 'n_dates': número de datas distintas
          - 'n_scenes': número total de cenas
        """
        search_window = 30
        start_date = target_date.strftime("%Y-%m-%d")
        end_date = (target_date + timedelta(days=search_window)).strftime("%Y-%m-%d")
        datetime_str = f"{start_date}T00:00:00Z/{end_date}T23:59:59Z"
    
        # Obter bbox do tile usando o dicionário local
        bbox = self.get_tile_bbox(tile)
        
        self.radar_log_output.append(f"Buscando imagens S1 para tile {tile}...")
        self.radar_log_output.append(f"BBox: {bbox}")
        QApplication.processEvents()
        
        catalog = pystac_client.Client.open("https://data.inpe.br/bdc/stac/v1/")
        
        # Buscar imagens Sentinel-1 usando bbox
        search = catalog.search(
            collections=["sentinel-1-rtc-1"],
            bbox=bbox,
            datetime=datetime_str
        )
        
        items = list(search.items())
        
        # Filtrar órbitas descendentes manualmente
        items = [item for item in items 
                 if item.properties.get('orbit_direction', '').upper() == 'DESCENDING']
        
        if not items:
            raise Exception(f"Nenhuma imagem Sentinel-1 encontrada para o tile {tile} na data {target_date}.")
    
        self.radar_log_output.append(f"Encontradas {len(items)} cenas no total.")
        QApplication.processEvents()
    
        # Extrai informações das cenas
        scene_urls = []
        scene_names = []
        orbits = []
        raw_datetimes = []
        for feat in items:
            url_vh = feat.assets['Gamma0_VH']['href']
            scene_urls.append(url_vh)
            scene_names.append(os.path.splitext(os.path.basename(url_vh))[0])
            orbits.append(str(feat.properties['relative_orbit']))
            raw_datetimes.append(feat.properties['datetime'])
    
        calendar_dates = sorted(set(datetime.fromisoformat(d).date() for d in raw_datetimes))
        self.radar_log_output.append(f"Datas de aquisição únicas: {len(calendar_dates)}")
        QApplication.processEvents()
    
        # Abre a primeira cena para obter o SRS original e definir o grid de trabalho
        ds_first = gdal.Open(f"/vsicurl/{scene_urls[0]}")
        src_srs = osr.SpatialReference()
        src_srs.ImportFromWkt(ds_first.GetProjection())
        tgt_srs = osr.SpatialReference()
        tgt_srs.ImportFromEPSG(4326)
        transform = osr.CoordinateTransformation(tgt_srs, src_srs)
    
        # Converte os cantos da BBOX para o SRS original
        minx, miny, maxx, maxy = bbox
        ulx, uly, _ = transform.TransformPoint(minx, maxy)
        lrx, lry, _ = transform.TransformPoint(maxx, miny)
        # Garante ordem correta para projWin (ulx < lrx, uly > lry)
        proj_win = [
            min(ulx, lrx),
            max(uly, lry),
            max(ulx, lrx),
            min(uly, lry)
        ]
        ds_first = None
    
        # Processamento de cada cena: crop, resample, leitura do array
        arrays = []
        template_geotransform = None
        template_projection = None
        template_shape = None
    
        for i, url in enumerate(scene_urls):
            self.radar_log_output.append(f"Processando cena {i+1}/{len(scene_urls)}: {scene_names[i]}")
            QApplication.processEvents()
    
            # Abre a cena e recorta para o tile
            mem_crop_path = f"/vsimem/crop_{i}.tif"
            src_ds = gdal.Open(f"/vsicurl/{url}")
            gdal.Translate(mem_crop_path, src_ds, projWin=proj_win)
            crop_ds = gdal.Open(mem_crop_path)
            
            if template_geotransform is None:
                template_geotransform = crop_ds.GetGeoTransform()
                template_projection = crop_ds.GetProjection()
                template_shape = (crop_ds.RasterYSize, crop_ds.RasterXSize)
            else:
                # Resample para o grid comum (bilinear)
                resampled_path = f"/vsimem/resampled_{i}.tif"
                gdal.Warp(resampled_path, crop_ds,
                          format='MEM',
                          xRes=template_geotransform[1],
                          yRes=abs(template_geotransform[5]),
                          outputBounds=[template_geotransform[0],
                                        template_geotransform[3] + template_geotransform[5] * template_shape[0],
                                        template_geotransform[0] + template_geotransform[1] * template_shape[1],
                                        template_geotransform[3]],
                          targetAlignedPixels=True,
                          resampleAlg='bilinear')
                crop_ds = None
                gdal.Unlink(mem_crop_path)
                crop_ds = gdal.Open(resampled_path)
                mem_crop_path = resampled_path  # para depois deletar
    
            array = crop_ds.GetRasterBand(1).ReadAsArray().astype(np.float32)
            arrays.append(array)
            crop_ds = None
            gdal.Unlink(mem_crop_path)
    
        self.radar_log_output.append("Recorte e alinhamento concluídos.")
        QApplication.processEvents()
    
        # --- Filtro por órbita (boxcar + razão + outcore) ---
        filtered_arrays = self.apply_orbit_filter(arrays, orbits)
    
        # --- Mosaico por data (média das cenas do mesmo dia) ---
        date_to_indices = {}
        for i, dt_str in enumerate(raw_datetimes):
            dt = datetime.fromisoformat(dt_str).date()
            if dt not in date_to_indices:
                date_to_indices[dt] = []
            date_to_indices[dt].append(i)
    
        date_rasters = []
        for dt in sorted(date_to_indices.keys()):
            idxs = date_to_indices[dt]
            if len(idxs) == 1:
                date_rasters.append(filtered_arrays[idxs[0]])
            else:
                stack = np.stack([filtered_arrays[i] for i in idxs], axis=0)
                mean_arr = np.mean(stack, axis=0)
                date_rasters.append(mean_arr)
    
        # --- Média temporal final ---
        final_array = np.mean(np.stack(date_rasters, axis=0), axis=0)
    
        return {
            'array': final_array,
            'geotransform': template_geotransform,
            'projection': template_projection,
            'dates': [d.strftime("%Y-%m-%d") for d in sorted(date_to_indices.keys())],
            'scenes': scene_names,
            'n_dates': len(date_to_indices),
            'n_scenes': len(scene_names)
        }

    def apply_orbit_filter(self, arrays, orbits):
        """
        Aplica o filtro por órbita: suavização boxcar 3x3, razão,
        média das razões por órbita e multiplicação.
        Retorna lista de arrays filtrados (mesma ordem).
        """
        n = len(arrays)
        smoothed = [uniform_filter(arr, size=3, mode='reflect').astype(np.float32) for arr in arrays]
        ratios = []
        for i in range(n):
            with np.errstate(divide='ignore', invalid='ignore'):
                rat = np.where(smoothed[i] > 0, arrays[i] / smoothed[i], 0.0)
            ratios.append(rat)

        filtered = [None] * n
        unique_orbits = set(orbits)
        for orb in unique_orbits:
            idx = [i for i, o in enumerate(orbits) if o == orb]
            stack_ratios = np.stack([ratios[i] for i in idx], axis=0)
            outcore = np.mean(stack_ratios, axis=0)
            for i in idx:
                filtered[i] = smoothed[i] * outcore
        return filtered

    def process_radar_image(self):
        """Processo principal da aba Imagem Radar."""
        self.radar_log_output.clear()
        year_str = self.radar_year_input.text().strip()
        tile = self.radar_tile_input.text().strip()
        folder = self.radar_folder_input.text().strip()

        if not year_str or not tile or not folder:
            QtWidgets.QMessageBox.warning(self, "Erro", "Preencha ano, tile e pasta de destino.")
            return
        try:
            year = int(year_str)
        except ValueError:
            QtWidgets.QMessageBox.warning(self, "Erro", "Ano inválido.")
            return
        if not re.match(r"^\d{6}$", tile):
            QtWidgets.QMessageBox.warning(self, "Erro", "Tile deve ter 6 dígitos (BBBPPP).")
            return
        if not os.path.exists(folder):
            os.makedirs(folder)

        ref_date = datetime(year, 7, 1).date()
        data_4m = ref_date - relativedelta(months=3)
        data_8m = ref_date - relativedelta(months=6)

        upper = -9
        lower = -18

        self.radar_log_output.append("===================================================")
        self.radar_log_output.append(f"Processando mosaico temporal Sentinel-1 para tile {tile}, ano {year}")
        self.radar_log_output.append("===================================================")
        QApplication.processEvents()

        mosaics = {}
        for label, date in [("current", ref_date), ("4m", data_4m), ("8m", data_8m)]:
            self.radar_log_output.append(f"\n--- Gerando mosaico {label} (referência {date}) ---")
            QApplication.processEvents()
            try:
                res = self.build_vh_mosaic(date, tile)
            except Exception as e:
                self.radar_log_output.append(f"Erro no mosaico {label}: {e}")
                QApplication.processEvents()
                return
            mosaics[label] = res
            self.radar_log_output.append(f"Datas usadas ({res['n_dates']}): {', '.join(res['dates'])}")
            self.radar_log_output.append(f"Total de cenas: {res['n_scenes']}")

        self.radar_log_output.append("\n--- Convertendo para dB, esticando e projetando... ---")
        QApplication.processEvents()

        projected_tifs = []
        for label in ["current", "4m", "8m"]:
            arr = mosaics[label]['array']
            gt = mosaics[label]['geotransform']
            proj = mosaics[label]['projection']

            db = 10 * np.log10(np.maximum(arr, 1e-10))
            db_clamped = np.clip(db, lower, upper)
            stretched = ((db_clamped - lower) / (upper - lower) * 254).astype(np.uint8)

            temp_stretch = os.path.join(folder, f"temp_{label}_stretch.tif")
            driver = gdal.GetDriverByName('GTiff')
            ds_tmp = driver.Create(temp_stretch, stretched.shape[1], stretched.shape[0], 1, gdal.GDT_Byte)
            ds_tmp.SetGeoTransform(gt)
            ds_tmp.SetProjection(proj)
            ds_tmp.GetRasterBand(1).WriteArray(stretched)
            ds_tmp.FlushCache()
            ds_tmp = None

            temp_proj = os.path.join(folder, f"temp_{label}_proj.tif")
            gdal.Warp(temp_proj, temp_stretch,
                      dstSRS='EPSG:10857',
                      xRes=10, yRes=10,
                      resampleAlg='near')
            projected_tifs.append(temp_proj)
            os.remove(temp_stretch)

        output_name = f"{tile}_VH_RGB_mean_temporal_10m.tif"
        output_path = os.path.join(folder, output_name)

        vrt_path = f"/vsimem/stack_vrt.vrt"
        gdal.BuildVRT(vrt_path, projected_tifs, separate=True)
        gdal.Translate(output_path, vrt_path,
                       creationOptions=['COMPRESS=DEFLATE', 'PREDICTOR=2', 'ZLEVEL=9'])
        gdal.Unlink(vrt_path)
        for p in projected_tifs:
            os.remove(p)

        raster_layer = QgsRasterLayer(output_path, output_name)
        if raster_layer.isValid():
            QgsProject.instance().addMapLayer(raster_layer)
            self.radar_log_output.append(f"✅ Camada adicionada ao QGIS: {output_name}")
        else:
            self.radar_log_output.append("⚠️ Imagem salva mas não pôde ser carregada no QGIS automaticamente.")

        report = [
            "===================================================",
            "Sentinel-1 Temporal RGB Report",
            "===================================================",
            f"Tile: {tile}",
            f"Data de processamento: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            ""
        ]
        for label, name in [("current", "ATUAL"), ("4m", "4 MESES"), ("8m", "8 MESES")]:
            m = mosaics[label]
            report += [
                f"---------------- {name} ----------------",
                f"Data de referência: {ref_date if label=='current' else (data_4m if label=='4m' else data_8m)}",
                f"Datas utilizadas: {', '.join(m['dates'])}",
                f"Número de datas: {m['n_dates']}",
                f"Número de cenas: {m['n_scenes']}",
                "",
                "Cenas:"
            ]
            report += [f"  - {s}" for s in m['scenes']]
            report.append("")

        report_path = os.path.join(folder, f"{tile}_report.txt")
        with open(report_path, 'w') as f:
            f.write('\n'.join(report))

        self.radar_log_output.append(f"✅ Relatório salvo: {report_path}")
        self.radar_log_output.append(">>> Processo concluído! <<<")
        QApplication.processEvents()


# ======================== CLASSE DO PLUGIN ========================
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
