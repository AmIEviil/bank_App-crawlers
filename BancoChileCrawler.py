from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from bs4 import BeautifulSoup
import hashlib
import re
import time
import json
import pika
import requests

class BancoChileCrawler:
    # ... (Mantén tu clase BancoChileCrawler EXACTAMENTE igual aquí) ...
    def __init__(self, rut, password):
        self.rut = rut
        self.password = password
        
        # Configuración para controlar Chrome
        options = webdriver.ChromeOptions()
        options.add_argument('--headless')
        options.add_argument('--disable-gpu')
        
        # 1. Forzar resolución explícita en lugar de start-maximized
        options.add_argument('--window-size=1920,1080')
        
        # 2. Ocultar la bandera de WebDriver para evadir anti-bots
        options.add_argument('--disable-blink-features=AutomationControlled')
        
        # 3. Falsificar el User-Agent para parecer un navegador normal
        options.add_argument('user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
        
        self.driver = webdriver.Chrome(options=options)
        self.wait = WebDriverWait(self.driver, 15) # Espera explícita para que cargue el DOM

    def login(self):
        # 1. Navegar al portal de personas del Banco de Chile
        self.driver.get('https://portales.bancochile.cl/personas')
        
        # 2. Hacer clic en "Banco en Línea" o el botón de ingreso
        # Buscamos por XPath el texto del enlace
        btn_ingreso = self.wait.until(
            EC.element_to_be_clickable((By.XPATH, "//a[contains(text(), 'Banco en Línea')]"))
        )
        btn_ingreso.click()

        # 3. Llenar el formulario
        input_rut = self.wait.until(
            EC.presence_of_element_located((By.ID, 'ppriv_per-login-click-input-rut'))
        )
        input_rut.send_keys(self.rut)

        input_pass = self.driver.find_element(By.ID, 'ppriv_per-login-click-input-password')
        input_pass.send_keys(self.password)

        btn_submit = self.driver.find_element(By.ID, 'ppriv_per-login-click-ingresar-login')
        btn_submit.click()
        
        # 4. Esperar a que el dashboard principal cargue (validación de login exitoso)
        btn_saldos = self.wait.until(
            EC.element_to_be_clickable((By.XPATH, "//button[contains(., 'SALDOS Y MOV. CUENTAS')]"))
        )
        btn_saldos.click()
        print("Login exitoso en Banco de Chile")

    def extract_transactions(self):
        # 1. Esperamos a que la tabla cargue en el DOM
        self.wait.until(
            EC.presence_of_element_located((By.CSS_SELECTOR, 'table.bch-table'))
        )
        
        # --- NUEVO: CAMBIAR RESULTADOS A 100 ---
        try:
            # Buscar el dropdown de "Resultados por página"
            dropdown_paginas = self.wait.until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "mat-select[aria-label='Resultados por página']"))
            )
            self.driver.execute_script("arguments[0].click();", dropdown_paginas)
            
            # Al hacer clic, Angular crea las opciones al final del DOM. Esperamos la opción "100"
            opcion_100 = self.wait.until(
                EC.element_to_be_clickable((By.XPATH, "//mat-option//span[contains(text(), '100')]"))
            )
            self.driver.execute_script("arguments[0].click();", opcion_100)
            
            # Le damos un pequeño respiro para que Angular destruya y reconstruya la tabla
            time.sleep(3) 
            print("Paginador cambiado a 100 resultados.")
        except Exception as e:
            print("No se pudo cambiar a 100 resultados, extrayendo con la vista por defecto.", e)
        # --------------------------------------

        all_transactions = []
        numero_pagina = 1
        
        # --- NUEVO: BUCLE DE PAGINACIÓN ---
        while True:
            print(f"Extrayendo página {numero_pagina}...")
            
            # Volvemos a leer el DOM (importante hacerlo en cada ciclo para tener la tabla actualizada)
            html = self.driver.page_source
            soup = BeautifulSoup(html, 'html.parser')
            
            rows = soup.find_all('tr', class_='bch-row')
            
            for row in rows:
                if 'table-collapse-row' in row.get('class', []):
                    continue

                cols = row.find_all('td')
                if not cols or len(cols) < 6:
                    continue

                fecha = cols[0].get_text(strip=True)
                descripcion = cols[1].get_text(strip=True)
                canal = cols[2].get_text(strip=True)
                
                cargo_texto = re.sub(r'[^\d]', '', cols[3].get_text(strip=True))
                abono_texto = re.sub(r'[^\d]', '', cols[4].get_text(strip=True))
                saldo_texto = re.sub(r'[^\d\-]', '', cols[5].get_text(strip=True))
                
                cargo = int(cargo_texto) if cargo_texto else 0
                abono = int(abono_texto) if abono_texto else 0
                saldo = int(saldo_texto) if saldo_texto and saldo_texto != '-' else 0

                monto = abono if abono > 0 else -cargo
                firma_str = f"{fecha}-{descripcion}-{monto}-{saldo}"
                firma = hashlib.sha1(firma_str.encode('utf-8')).hexdigest()

                all_transactions.append({
                    'date': fecha,
                    'description': descripcion,
                    'channel': canal,
                    'amount': monto,
                    'balance': saldo,
                    'signature': firma
                })
            
            # --- EVALUAR SI HAY SIGUIENTE PÁGINA ---
            try:
                btn_siguiente = self.driver.find_element(By.CSS_SELECTOR, "button.mat-paginator-navigation-next")
                
                # En Angular, si no hay más páginas, el botón tiene 'disabled' o la clase 'mat-button-disabled'
                clases_btn = btn_siguiente.get_attribute('class')
                is_disabled = btn_siguiente.get_attribute('disabled')
                
                if 'mat-button-disabled' in clases_btn or is_disabled == 'true':
                    print("Última página alcanzada.")
                    break # Salimos del bucle while
                
                # Si llegamos aquí, hay otra página. Hacemos clic.
                self.driver.execute_script("arguments[0].click();", btn_siguiente)
                numero_pagina += 1
                
                # Pausa para dejar que Angular recargue la tabla
                time.sleep(3)
                
            except Exception as e:
                print("No se encontró el botón de siguiente o hubo un error:", e)
                break
                
        return all_transactions

    def close(self):
        self.driver.quit()

# --- NUEVA SECCIÓN: CONFIGURACIÓN DE RABBITMQ ---

# Endpoint de backend
ENDPOINT_DESTINO = 'http://localhost:3000/api/banco-chile/save-data'

def procesar_mensaje(ch, method, properties, body):
    print(" [x] Mensaje recibido de la cola.")
    
    try:
        # 1. Decodificar el mensaje JSON
        mensaje_str = body.decode('utf-8')
        mensaje_json = json.loads(mensaje_str)
        
        # 2. Verificar que el patrón coincida con lo que manda NestJS
        if mensaje_json.get('pattern') == 'procesar_transaccion':
            data = mensaje_json.get('data', {})
            rut = data.get('rut')
            password = data.get('password')
            
            print(f" [>] Iniciando crawler para RUT: {rut}")
            
            # 3. Iniciar el crawler con las credenciales extraídas
            scraper = BancoChileCrawler(rut, password)
            
            scraper.login()
            movimientos = scraper.extract_transactions()
            
            # 4. Enviar los datos al endpoint de NestJS (o donde necesites)
            payload = {
                "rut": rut,
                "movimientos": movimientos
            }
            
            print(f" [>] Enviando {len(movimientos)} movimientos al backend...")
            respuesta = requests.post(ENDPOINT_DESTINO, json=payload)
            
            if respuesta.status_code in [200, 201]:
                print(" [v] Datos enviados correctamente.")
            else:
                print(f" [!] Error al enviar datos: {respuesta.status_code} - {respuesta.text}")
                
            # Cierra el navegador una vez terminado el proceso
            scraper.close()
            
            # 5. Confirmar a RabbitMQ que el mensaje fue procesado exitosamente (Acknowledgment)
            ch.basic_ack(delivery_tag=method.delivery_tag)
            print(" [x] Proceso finalizado y mensaje removido de la cola.\n")
            
    except Exception as e:
        print(f" [X] Error durante el procesamiento: {e}")
        # En caso de error crítico, rechazamos el mensaje para que vuelva a la cola o se descarte
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        try:
            scraper.close()
        except:
            pass

def iniciar_consumidor():
    # Conexión al servidor RabbitMQ (ajusta 'localhost' si usas otra IP)
    conexion = pika.BlockingConnection(pika.ConnectionParameters('localhost'))
    canal = conexion.channel()

    # Declarar la cola (durable=True coincide con la configuración que pusiste en NestJS)
    canal.queue_declare(queue='cola-banco-chile', durable=True)

    # basic_qos(prefetch_count=1) asegura que el worker tome de a 1 mensaje a la vez,
    # ideal para procesos pesados como Selenium
    canal.basic_qos(prefetch_count=1)

    # Configurar qué función se ejecutará cuando llegue un mensaje
    canal.basic_consume(
        queue='cola-banco-chile', 
        on_message_callback=procesar_mensaje
    )

    print(' [*] Esperando mensajes en "cola-banco-chile". Para salir presiona CTRL+C')
    canal.start_consuming()

if __name__ == "__main__":
    try:
        iniciar_consumidor()
    except KeyboardInterrupt:
        print('Interrumpido por el usuario.')