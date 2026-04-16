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

CLICK_JS = "arguments[0].click();"
NON_DIGIT_PATTERN = r'[^\d]'

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

    def _extract_first_integer(self, text):
        match = re.search(r'\d+', (text or '').strip())
        if match:
            return int(match.group(0))
        return None

    def _set_results_per_page(self):
        try:
            dropdown_paginas = None
            paginator = self._find_visible_paginator()
            dropdowns = []

            if paginator is not None:
                dropdowns = paginator.find_elements(
                    By.CSS_SELECTOR,
                    "mat-select[aria-label='Resultados por página'], .mat-paginator-select mat-select",
                )

            if not dropdowns:
                dropdowns = self.driver.find_elements(
                    By.CSS_SELECTOR,
                    "mat-select[aria-label='Resultados por página']",
                )

            for dropdown in dropdowns:
                if dropdown.is_displayed() and dropdown.is_enabled():
                    dropdown_paginas = dropdown
                    break

            if dropdown_paginas is None:
                return None

            self.driver.execute_script(CLICK_JS, dropdown_paginas)
            self.wait.until(
                EC.presence_of_all_elements_located(
                    (By.CSS_SELECTOR, "mat-option[role='option']")
                )
            )

            opciones = [
                opcion
                for opcion in self.driver.find_elements(
                    By.CSS_SELECTOR,
                    "mat-option[role='option']",
                )
                if opcion.is_displayed()
            ]
            if not opciones:
                return None

            # Seleccionar siempre la última opción visible para adaptarse a cada vista.
            opcion_objetivo = opciones[-1]
            valor_objetivo = self._extract_first_integer(opcion_objetivo.text)

            self.driver.execute_script(CLICK_JS, opcion_objetivo)
            time.sleep(2)
            return valor_objetivo
        except Exception:
            return None

    def _find_visible_paginator(self):
        paginator_selectors = [
            "bch-paginator.mat-paginator",
            "div.bch-paginator",
            "[role='group'].bch-paginator",
        ]

        for selector in paginator_selectors:
            paginators = self.driver.find_elements(By.CSS_SELECTOR, selector)
            for paginator in paginators:
                try:
                    if paginator.is_displayed():
                        return paginator
                except Exception:
                    continue

        return None

    def _parse_amount(self, raw_text):
        text = (raw_text or '').strip()
        if not text:
            return 0.0

        negative = '-' in text or ('(' in text and ')' in text)
        number_text = re.sub(r'[^0-9,.-]', '', text)

        if not number_text:
            return 0.0

        number_text = self._normalize_amount_string(number_text)
        amount = self._safe_float(number_text, text)

        if negative and amount > 0:
            amount *= -1

        return amount

    def _normalize_text(self, value):
        return (
            (value or '')
            .strip()
            .lower()
            .replace('á', 'a')
            .replace('é', 'e')
            .replace('í', 'i')
            .replace('ó', 'o')
            .replace('ú', 'u')
        )

    def _detect_currency_from_text(self, text, default_currency='CLP'):
        upper_text = (text or '').strip().upper()
        if 'USD' in upper_text or 'US$' in upper_text:
            return 'USD'

        if 'CLP' in upper_text or '$' in upper_text:
            return 'CLP'

        return default_currency

    def _parse_amount_with_currency(self, raw_text, default_currency='CLP'):
        return (
            abs(self._parse_amount(raw_text)),
            self._detect_currency_from_text(raw_text, default_currency),
        )

    def _default_currency_context(self, scope_key):
        currency = 'USD' if scope_key == 'internacional' else 'CLP'
        return {
            'cargoCurrency': currency,
            'pagoCurrency': currency,
            'originCurrency': currency,
        }

    def _extract_table_currency_context(self, sample_row, scope_key):
        context = self._default_currency_context(scope_key)

        try:
            table = sample_row.find_element(By.XPATH, "ancestor::table[1]")
            headers = table.find_elements(By.CSS_SELECTOR, 'thead th')

            for header in headers:
                header_classes = header.get_attribute('class') or ''
                header_text = header.text.strip()

                if (
                    'cdk-column-montoCargo' in header_classes
                    or 'cdk-column-cargo' in header_classes
                    or 'cdk-column-cargoUSD' in header_classes
                ):
                    context['cargoCurrency'] = self._detect_currency_from_text(
                        header_text,
                        context['cargoCurrency'],
                    )

                if (
                    'cdk-column-montoPago' in header_classes
                    or 'cdk-column-pago' in header_classes
                    or 'cdk-column-pagoUSD' in header_classes
                ):
                    context['pagoCurrency'] = self._detect_currency_from_text(
                        header_text,
                        context['pagoCurrency'],
                    )

                if 'cdk-column-montoMonedaOrigen' in header_classes:
                    context['originCurrency'] = self._detect_currency_from_text(
                        header_text,
                        context['originCurrency'],
                    )
        except Exception:
            return context

        return context

    def _extract_facturado_summary_for_scope(self, scope_label, scope_key):
        summary = {
            'montoFacturado': 0.0,
            'monedaFacturada': 'USD' if scope_key == 'internacional' else 'CLP',
            'fechaFacturacion': '',
            'pagarHasta': '',
            'pagoMinimo': 0.0,
        }

        target_scope = self._normalize_text(scope_label)
        cards = self.driver.find_elements(
            By.CSS_SELECTOR,
            "div.bch-summary.movimientos-facturados",
        )

        for card in cards:
            try:
                if not card.is_displayed():
                    continue

                title = card.find_element(
                    By.CSS_SELECTOR,
                    ".summary-header-title h3",
                ).text.strip()
                if target_scope not in self._normalize_text(title):
                    continue

                billed_text = card.find_element(
                    By.CSS_SELECTOR,
                    ".summary-header-lead .number",
                ).text.strip()
                billed_amount, billed_currency = self._parse_amount_with_currency(
                    billed_text,
                    summary['monedaFacturada'],
                )
                summary['montoFacturado'] = billed_amount
                summary['monedaFacturada'] = billed_currency

                rows = card.find_elements(
                    By.CSS_SELECTOR,
                    ".summary-body .row.jc-sb",
                )
                for row in rows:
                    label = row.find_element(By.CSS_SELECTOR, 'p.list-item').text.strip()
                    value = row.find_element(By.CSS_SELECTOR, 'span.number').text.strip()
                    normalized_label = self._normalize_text(label)

                    if 'fecha de facturacion' in normalized_label:
                        summary['fechaFacturacion'] = value
                    elif 'pagar hasta' in normalized_label:
                        summary['pagarHasta'] = value
                    elif 'pago minimo' in normalized_label:
                        minimum_payment, _ = self._parse_amount_with_currency(
                            value,
                            summary['monedaFacturada'],
                        )
                        summary['pagoMinimo'] = minimum_payment

                return summary
            except Exception:
                continue

        return summary

    def _normalize_amount_string(self, number_text):
        normalized = number_text

        if ',' in normalized and '.' in normalized:
            if normalized.rfind(',') > normalized.rfind('.'):
                return normalized.replace('.', '').replace(',', '.')
            return normalized.replace(',', '')

        if ',' in normalized and '.' not in normalized:
            return normalized.replace('.', '').replace(',', '.')

        if '.' in normalized and ',' not in normalized:
            parts = normalized.split('.')
            if len(parts) > 2 or (len(parts) == 2 and len(parts[1]) == 3):
                return ''.join(parts)

        return normalized

    def _safe_float(self, number_text, fallback_text=''):
        try:
            return float(number_text)
        except ValueError:
            only_digits = re.sub(NON_DIGIT_PATTERN, '', fallback_text)
            return float(only_digits) if only_digits else 0.0

    def _click_first_visible(self, xpaths, timeout_seconds=15):
        end_time = time.time() + timeout_seconds

        while time.time() < end_time:
            for xpath in xpaths:
                elementos = self.driver.find_elements(By.XPATH, xpath)
                for elemento in elementos:
                    try:
                        if not elemento.is_displayed() or not elemento.is_enabled():
                            continue

                        self.driver.execute_script(
                            "arguments[0].scrollIntoView({block: 'center'});",
                            elemento,
                        )
                        self.driver.execute_script(CLICK_JS, elemento)
                        return True
                    except Exception:
                        continue

            time.sleep(0.5)

        return False

    def _find_visible_next_button(self):
        end_time = time.time() + 4

        while time.time() < end_time:
            paginator = self._find_visible_paginator()
            if paginator is not None:
                botones = paginator.find_elements(
                    By.CSS_SELECTOR,
                    "button.mat-paginator-navigation-next, button[aria-label='Próxima página'], button[aria-label='Proxima página']",
                )

                for boton in botones:
                    try:
                        if boton.is_displayed():
                            return boton
                    except Exception:
                        continue

            time.sleep(0.4)

        return None

    def _advance_to_next_page(self, context_label=''):
        btn_siguiente = self._find_visible_next_button()
        if btn_siguiente is None:
            if self._find_visible_paginator() is not None:
                return False

            if context_label:
                print(f"No se encontró paginador visible para {context_label}.")
            else:
                print("No se encontró paginador visible.")
            return False

        clases_btn = btn_siguiente.get_attribute('class') or ''
        is_disabled = btn_siguiente.get_attribute('disabled')

        if 'mat-button-disabled' in clases_btn or is_disabled == 'true':
            return False

        self.driver.execute_script(CLICK_JS, btn_siguiente)
        time.sleep(3)
        return True

    def _parse_installments(self, installments_text):
        match = re.search(r'^(\d{1,3})\s*/\s*(\d{1,3})$', (installments_text or '').strip())
        if not match:
            return None, None

        installment_number = int(match.group(1))
        installment_total = int(match.group(2))
        return installment_number, installment_total

    def _get_no_facturados_billing_date(self):
        month_map = {
            'enero': 1,
            'febrero': 2,
            'marzo': 3,
            'abril': 4,
            'mayo': 5,
            'junio': 6,
            'julio': 7,
            'agosto': 8,
            'septiembre': 9,
            'setiembre': 9,
            'octubre': 10,
            'noviembre': 11,
            'diciembre': 12,
        }

        try:
            hints = self.driver.find_elements(By.XPATH, "//*[contains(., 'facturados')]")
            for hint in hints:
                if not hint.is_displayed():
                    continue

                text = hint.text.strip().lower()
                if 'facturados' not in text:
                    continue

                match = re.search(r'(\d{1,2})\s+de\s+([a-záéíóúñ]+)', text)
                if not match:
                    continue

                day = int(match.group(1))
                month_name = (
                    match.group(2)
                    .replace('á', 'a')
                    .replace('é', 'e')
                    .replace('í', 'i')
                    .replace('ó', 'o')
                    .replace('ú', 'u')
                )
                month = month_map.get(month_name)
                if month is None:
                    continue

                year = time.localtime().tm_year
                return f"{day:02d}/{month:02d}/{year}"
        except Exception:
            return ''

        return ''

    def _ensure_scope_tabs_visible(self):
        try:
            self.wait.until(
                EC.presence_of_element_located(
                    (
                        By.XPATH,
                        "//div[contains(@class, 'mat-tab-label-content') and contains(., 'Nacional')]",
                    )
                )
            )
            self.wait.until(
                EC.presence_of_element_located(
                    (
                        By.XPATH,
                        "//div[contains(@class, 'mat-tab-label-content') and contains(., 'Internacional')]",
                    )
                )
            )
            return True
        except Exception:
            return False

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
            selected_size = self._set_results_per_page()
            if selected_size is not None:
                print(f"Paginador ajustado al maximo disponible ({selected_size} resultados).")
            else:
                print("No se encontró paginador visible. Se usará vista por defecto.")
        except Exception as e:
            print("No se pudo cambiar paginación, extrayendo con la vista por defecto.", e)
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
                parsed_row = self._parse_account_row(row)
                if parsed_row:
                    all_transactions.append(parsed_row)
            
            # --- EVALUAR SI HAY SIGUIENTE PÁGINA ---
            try:
                if not self._advance_to_next_page():
                    print("Última página alcanzada.")
                    break # Salimos del bucle while

                numero_pagina += 1
            except Exception as e:
                print("No se encontró el botón de siguiente o hubo un error:", e)
                break
                
        return all_transactions

    def _parse_account_row(self, row):
        if 'table-collapse-row' in row.get('class', []):
            return None

        cols = row.find_all('td')
        if not cols or len(cols) < 6:
            return None

        fecha = cols[0].get_text(strip=True)
        descripcion = cols[1].get_text(strip=True)
        canal = cols[2].get_text(strip=True)

        cargo_texto = re.sub(NON_DIGIT_PATTERN, '', cols[3].get_text(strip=True))
        abono_texto = re.sub(NON_DIGIT_PATTERN, '', cols[4].get_text(strip=True))
        saldo_texto = re.sub(r'[^\d\-]', '', cols[5].get_text(strip=True))

        cargo = int(cargo_texto) if cargo_texto else 0
        abono = int(abono_texto) if abono_texto else 0
        saldo = int(saldo_texto) if saldo_texto and saldo_texto != '-' else 0

        monto = abono if abono > 0 else -cargo
        firma_str = f"{fecha}-{descripcion}-{monto}-{saldo}"
        firma = hashlib.sha1(firma_str.encode('utf-8')).hexdigest()

        return {
            'date': fecha,
            'description': descripcion,
            'channel': canal,
            'amount': monto,
            'balance': saldo,
            'signature': firma,
        }

    def _open_credit_card_section(self):
        xpaths_tarjeta = [
            "//section[.//h3[contains(normalize-space(.), 'Accesos Directos')]]//span[contains(@class, 'btn-text') and contains(normalize-space(.), 'SALDOS Y MOV.TARJETAS CRÉDITO')]/ancestor::button[1]",
            "//section[.//h3[contains(normalize-space(.), 'Accesos Directos')]]//span[contains(@class, 'btn-text') and contains(normalize-space(.), 'SALDOS Y MOV.TARJETAS CREDITO')]/ancestor::button[1]",
            "//div[contains(@class, 'linkAccesoDirecto')]//span[contains(@class, 'btn-text') and contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZÁÉÍÓÚ', 'abcdefghijklmnopqrstuvwxyzáéíóú'), 'saldos y mov.tarjetas crédito')]/ancestor::button[1]",
            "//div[contains(@class, 'linkAccesoDirecto')]//span[contains(@class, 'btn-text') and contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZÁÉÍÓÚ', 'abcdefghijklmnopqrstuvwxyzáéíóú'), 'saldos y mov.tarjetas credito')]/ancestor::button[1]",
            "//button[contains(., 'SALDOS Y MOV.TARJETAS CRÉDITO')]",
            "//button[contains(., 'SALDOS Y MOV.TARJETAS CREDITO')]",
            "//span[contains(., 'SALDOS Y MOV.TARJETAS CRÉDITO')]/ancestor::button[1]",
            "//span[contains(., 'SALDOS Y MOV.TARJETAS CREDITO')]/ancestor::button[1]",
            "//button[contains(., 'Saldos y Mov.Tarjetas Crédito')]",
            "//button[contains(., 'Saldos y Mov.Tarjetas Credito')]",
        ]

        if not self._click_first_visible(xpaths_tarjeta, timeout_seconds=20):
            print("No se encontró botón de Tarjetas en la vista actual. Intentando acceso directo por URL.")
            current_url = self.driver.current_url
            if '#/' in current_url:
                target_url = f"{current_url.split('#')[0]}#/tarjeta-credito/consultar/saldos"
            else:
                target_url = 'https://www.mibancochile.cl/mibancochile-web/front/persona/index.html#/tarjeta-credito/consultar/saldos'
            self.driver.get(target_url)

        self.wait.until(
            EC.presence_of_element_located(
                (
                    By.XPATH,
                    "//a[contains(@class, 'mat-tab-link') and (contains(., 'no facturados') or contains(., 'facturados'))]",
                )
            )
        )

    def _select_statement_tab(self, statement_key):
        if statement_key == 'no_facturados':
            xpaths = [
                "//a[contains(@class, 'mat-tab-link') and contains(., 'Saldos y movimientos no facturados')]",
                "//a[contains(@class, 'mat-tab-link') and contains(., 'no facturados')]",
            ]
        else:
            xpaths = [
                "//a[contains(@class, 'mat-tab-link') and contains(., 'Movimientos facturados')]",
            ]

        clicked = self._click_first_visible(xpaths, timeout_seconds=10)
        if clicked:
            time.sleep(2)
            self._ensure_scope_tabs_visible()
        return clicked

    def _select_scope_tab(self, scope_label):
        xpaths = [
            f"//div[@role='tab' and .//div[contains(@class, 'mat-tab-label-content') and contains(., '{scope_label}')]]",
            f"//div[contains(@class, 'mat-tab-label') and .//div[contains(@class, 'mat-tab-label-content') and contains(., '{scope_label}')]]",
        ]

        clicked = self._click_first_visible(xpaths, timeout_seconds=10)
        if clicked:
            time.sleep(2)
        return clicked

    def _extract_credit_row_fields(self, cols):
        field_map = {
            'cdk-column-fechaTransaccion': ('fecha', 'text'),
            'cdk-column-fechaCompra': ('fecha_compra', 'text'),
            'cdk-column-fechaFacturacion': ('fecha_facturacion', 'text'),
            'cdk-column-glosaTransaccion': ('descripcion', 'text'),
            'cdk-column-descripcion': ('descripcion', 'text'),
            'cdk-column-tipoTarjeta': ('tipo_tarjeta', 'text'),
            'cdk-column-tipoMovimientoLabel': ('tipo_movimiento', 'text'),
            'cdk-column-ciudad': ('ciudad', 'text'),
            'cdk-column-codigoPaisComercio': ('pais', 'text'),
            'cdk-column-pais': ('pais', 'text'),
            'cdk-column-despliegueCuotas': ('cuotas', 'text'),
            'cdk-column-cuotas': ('cuotas', 'text'),
            'cdk-column-valorCuota': ('valor_cuota', 'amount'),
            'cdk-column-montoTotal': ('monto_total', 'amount'),
            'cdk-column-montoMonedaOrigen': ('monto_moneda_origen', 'amount'),
            'cdk-column-montoCargo': ('cargo', 'amount'),
            'cdk-column-montoPago': ('pago', 'amount'),
            'cdk-column-cargo': ('cargo', 'amount'),
            'cdk-column-pago': ('pago', 'amount'),
            'cdk-column-cargoUSD': ('cargo', 'amount'),
            'cdk-column-pagoUSD': ('pago', 'amount'),
        }

        data = {
            'fecha': '',
            'fecha_compra': '',
            'fecha_facturacion': '',
            'descripcion': '',
            'tipo_tarjeta': '',
            'tipo_movimiento': '',
            'ciudad': '',
            'pais': '',
            'cuotas': '',
            'cargo': 0.0,
            'pago': 0.0,
            'valor_cuota': 0.0,
            'monto_total': 0.0,
            'monto_moneda_origen': 0.0,
        }

        for col in cols:
            classes = col.get_attribute('class') or ''
            value = col.text.strip()
            for class_token, (field_name, field_type) in field_map.items():
                if class_token not in classes:
                    continue

                if field_type == 'amount':
                    data[field_name] = abs(self._parse_amount(value))
                else:
                    data[field_name] = value
                break

        return data

    def _is_credit_card_payment(self, data, amounts):
        if amounts['pago'] > 0 and amounts['cargo'] <= 0:
            return True

        movement_type = self._normalize_text(data.get('tipo_movimiento'))
        description = self._normalize_text(data.get('descripcion'))

        if 'pago' in movement_type:
            return True

        if description.startswith('pago ') and amounts['pago'] > 0:
            return True

        return False

    def _find_date_in_columns(self, cols):
        for col in cols:
            possible_date = col.text.strip()
            if re.match(r'^\d{2}/\d{2}/\d{4}$', possible_date):
                return possible_date
        return ''

    def _resolve_credit_row_identity(self, cols, data, statement_key, billing_date_hint):
        fecha = data['fecha'] or self._find_date_in_columns(cols)
        descripcion = data['descripcion']

        if not descripcion and len(cols) >= 3:
            descripcion = cols[2].text.strip()

        if not fecha and not descripcion:
            return None

        fecha_compra = data['fecha_compra'] or fecha
        fecha_facturacion = data['fecha_facturacion']

        if not fecha_facturacion:
            if billing_date_hint:
                fecha_facturacion = billing_date_hint
            elif statement_key == 'facturados':
                fecha_facturacion = fecha_compra

        return {
            'fechaCompra': fecha_compra,
            'fechaFacturacion': fecha_facturacion,
            'descripcion': descripcion,
        }

    def _resolve_credit_row_amounts(self, data):
        cargo = data['cargo']
        pago = data['pago']
        cuotas = data['cuotas']
        valor_cuota = data['valor_cuota']
        monto_total = data['monto_total']

        cuota_numero, cuota_total = self._parse_installments(cuotas)
        if valor_cuota <= 0:
            valor_cuota = cargo if cargo > 0 else pago

        if monto_total <= 0:
            if cuota_total and cuota_total > 0 and valor_cuota > 0:
                monto_total = valor_cuota * cuota_total
            else:
                monto_total = valor_cuota

        monto_neto = pago if pago > 0 else -cargo

        return {
            'cargo': cargo,
            'pago': pago,
            'montoNeto': monto_neto,
            'cuotaNumero': cuota_numero,
            'cuotaTotal': cuota_total,
            'valorCuota': valor_cuota,
            'montoTotal': monto_total,
        }

    def _build_credit_signature(self, identity, statement_key, scope_key, data, amounts, currency_context):
        signature_source = (
            f"{identity['fechaCompra']}-{identity['fechaFacturacion']}-"
            f"{identity['descripcion']}-{amounts['montoNeto']}-{statement_key}-{scope_key}-"
            f"{data['tipo_tarjeta']}-{data['tipo_movimiento']}-{data['pais']}-{data['ciudad']}-{data['cuotas']}-"
            f"{amounts['valorCuota']}-{amounts['montoTotal']}-{amounts['cargo']}-{amounts['pago']}-"
            f"{currency_context['cargoCurrency']}-{currency_context['pagoCurrency']}"
        )
        return hashlib.sha1(signature_source.encode('utf-8')).hexdigest()

    def _build_credit_payload(self, statement_key, scope_key, identity, data, amounts, currency_context, facturado_summary, signature):
        is_payment = self._is_credit_card_payment(data, amounts)
        movement_kind = 'payment' if is_payment else 'charge'
        main_currency = (
            currency_context['pagoCurrency'] if is_payment else currency_context['cargoCurrency']
        )

        return {
            # Campos base para compatibilidad con el backend actual
            'date': identity['fechaCompra'],
            'description': identity['descripcion'],
            'channel': f"BANCO_CHILE_TC_{scope_key.upper()}_{statement_key.upper()}",
            'amount': round(amounts['montoNeto'], 2),
            'balance': 0,

            # Interfaz específica de tarjeta de crédito (v1)
            'recordType': 'credit_card_movement',
            'interfaceVersion': 'tc-v1',
            'statementType': statement_key,
            'scope': scope_key,
            'fechaCompra': identity['fechaCompra'],
            'fechaFacturacion': identity['fechaFacturacion'],
            'fechaVencimientoPago': facturado_summary.get('pagarHasta', ''),
            'tarjeta': data['tipo_tarjeta'],
            'tipoMovimiento': data['tipo_movimiento'],
            'pais': data['pais'],
            'ciudad': data['ciudad'],
            'cuotas': data['cuotas'],
            'cuotaNumero': amounts['cuotaNumero'],
            'cuotaTotal': amounts['cuotaTotal'],
            'valorCuota': round(amounts['valorCuota'], 2),
            'montoTotal': round(amounts['montoTotal'], 2),
            'montoMonedaOrigen': round(data['monto_moneda_origen'], 2),
            'montoCargo': round(amounts['cargo'], 2),
            'montoPago': round(amounts['pago'], 2),
            'montoNeto': round(amounts['montoNeto'], 2),
            'cargoCurrency': currency_context['cargoCurrency'],
            'pagoCurrency': currency_context['pagoCurrency'],
            'originCurrency': currency_context['originCurrency'],
            'currency': main_currency,
            'movementKind': movement_kind,
            'isPayment': is_payment,
            'montoFacturadoResumen': round(
                facturado_summary.get('montoFacturado', 0.0),
                2,
            ),
            'monedaFacturadaResumen': facturado_summary.get('monedaFacturada', main_currency),
            'pagoMinimoResumen': round(facturado_summary.get('pagoMinimo', 0.0), 2),

            # Copia plana adicional por trazabilidad
            'cardType': data['tipo_tarjeta'],
            'movementType': data['tipo_movimiento'],
            'country': data['pais'],
            'city': data['ciudad'],
            'installments': data['cuotas'],
            'signature': signature,
        }

    def _extract_credit_row(self, row, statement_key, scope_key, currency_context, facturado_summary, billing_date_hint=''):
        cols = row.find_elements(By.CSS_SELECTOR, 'td')
        if not cols:
            return None

        data = self._extract_credit_row_fields(cols)

        identity = self._resolve_credit_row_identity(
            cols,
            data,
            statement_key,
            billing_date_hint,
        )
        if identity is None:
            return None

        amounts = self._resolve_credit_row_amounts(data)
        signature = self._build_credit_signature(
            identity,
            statement_key,
            scope_key,
            data,
            amounts,
            currency_context,
        )

        return self._build_credit_payload(
            statement_key,
            scope_key,
            identity,
            data,
            amounts,
            currency_context,
            facturado_summary,
            signature,
        )

    def _extract_credit_scope_transactions(self, statement_key, scope_key, scope_label):
        try:
            selected_size = self._set_results_per_page()
            if selected_size is not None:
                print(
                    f"Paginador de {statement_key}/{scope_key} ajustado al maximo disponible ({selected_size})."
                )
        except Exception:
            pass

        facturado_summary = {
            'montoFacturado': 0.0,
            'monedaFacturada': 'USD' if scope_key == 'internacional' else 'CLP',
            'fechaFacturacion': '',
            'pagarHasta': '',
            'pagoMinimo': 0.0,
        }

        if statement_key == 'facturados':
            facturado_summary = self._extract_facturado_summary_for_scope(
                scope_label,
                scope_key,
            )

        billing_date_hint = ''
        if statement_key == 'facturados':
            billing_date_hint = facturado_summary.get('fechaFacturacion', '')
        elif statement_key == 'no_facturados':
            billing_date_hint = self._get_no_facturados_billing_date()

        movimientos = []
        numero_pagina = 1
        max_paginas = 50

        while numero_pagina <= max_paginas:
            print(f"Extrayendo TC {statement_key}/{scope_key} - página {numero_pagina}...")

            movimientos.extend(
                self._collect_credit_rows_from_current_page(
                    statement_key,
                    scope_key,
                    facturado_summary,
                    billing_date_hint,
                )
            )

            if not self._advance_to_next_page(f"{statement_key}/{scope_key}"):
                print(f"Última página para {statement_key}/{scope_key}.")
                break

            numero_pagina += 1

        return movimientos

    def _is_valid_credit_row_element(self, row):
        if not row.is_displayed():
            return False

        row_classes = row.get_attribute('class') or ''
        return 'table-collapse-row' not in row_classes

    def _resolve_currency_context_for_rows(self, rows, scope_key):
        currency_context = self._default_currency_context(scope_key)

        for row in rows:
            try:
                if not self._is_valid_credit_row_element(row):
                    continue

                currency_context = self._extract_table_currency_context(row, scope_key)
                break
            except Exception:
                continue

        return currency_context

    def _collect_credit_rows_from_current_page(self, statement_key, scope_key, facturado_summary, billing_date_hint):
        extracted_rows = []
        rows = self.driver.find_elements(By.CSS_SELECTOR, 'tr.bch-row')
        currency_context = self._resolve_currency_context_for_rows(rows, scope_key)

        for row in rows:
            try:
                if not self._is_valid_credit_row_element(row):
                    continue

                movimiento = self._extract_credit_row(
                    row,
                    statement_key,
                    scope_key,
                    currency_context,
                    facturado_summary,
                    billing_date_hint=billing_date_hint,
                )
                if movimiento:
                    extracted_rows.append(movimiento)
            except Exception:
                continue

        return extracted_rows

    def extract_credit_card_transactions(self):
        self._open_credit_card_section()

        statement_tabs = [
            ('no_facturados', 'Saldos y movimientos no facturados'),
            ('facturados', 'Movimientos facturados'),
        ]
        scope_tabs = [
            ('nacional', 'Nacional'),
            ('internacional', 'Internacional'),
        ]

        all_credit_transactions = []

        for statement_key, statement_label in statement_tabs:
            print(f"Procesando vista: {statement_label}")

            if not self._select_statement_tab(statement_key):
                print(f"No se pudo abrir la vista {statement_label}. Se omite.")
                continue

            if not self._ensure_scope_tabs_visible():
                print(f"No se encontraron tabs Nacional/Internacional en {statement_label}. Se omite.")
                continue

            for scope_key, scope_label in scope_tabs:
                print(f"Procesando sub-tab: {scope_label} ({statement_label})")

                if not self._select_scope_tab(scope_label):
                    print(f"No se pudo abrir sub-tab {scope_label}. Se omite.")
                    continue

                movimientos_scope = self._extract_credit_scope_transactions(
                    statement_key,
                    scope_key,
                    scope_label,
                )
                all_credit_transactions.extend(movimientos_scope)

        return all_credit_transactions

    def logout(self):
        logout_xpaths = [
            "//button[contains(@class, 'button-logout') and .//span[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZÁÉÍÓÚ', 'abcdefghijklmnopqrstuvwxyzáéíóú'), 'cerrar sesión')]]",
            "//button[contains(@class, 'button-logout') and .//span[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZÁÉÍÓÚ', 'abcdefghijklmnopqrstuvwxyzáéíóú'), 'cerrar sesion')]]",
            "//button[contains(@class, 'button-logout')]",
            "//span[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZÁÉÍÓÚ', 'abcdefghijklmnopqrstuvwxyzáéíóú'), 'cerrar sesión')]/ancestor::button[1]",
            "//span[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZÁÉÍÓÚ', 'abcdefghijklmnopqrstuvwxyzáéíóú'), 'cerrar sesion')]/ancestor::button[1]",
        ]

        if not self._click_first_visible(logout_xpaths, timeout_seconds=15):
            print("No se encontró botón de cerrar sesión.")
            return False

        end_time = time.time() + 20
        while time.time() < end_time:
            try:
                if self.driver.find_elements(By.ID, 'ppriv_per-login-click-input-rut'):
                    print("Sesión cerrada correctamente.")
                    return True

                if self.driver.find_elements(By.XPATH, "//a[contains(text(), 'Banco en Línea')]"):
                    print("Sesión cerrada correctamente.")
                    return True
            except Exception:
                pass

            time.sleep(0.5)

        print("No se pudo confirmar cierre de sesión. Se continúa con el proceso.")
        return False

    def close(self):
        self.driver.quit()

# --- NUEVA SECCIÓN: CONFIGURACIÓN DE RABBITMQ ---

# Endpoint de backend
ENDPOINT_DESTINO = 'http://localhost:3000/api/banco-chile/save-data'

def enviar_payload_backend(payload, etiqueta, cantidad):
    print(f" [>] Enviando {cantidad} movimientos de {etiqueta} al backend...")
    respuesta = requests.post(ENDPOINT_DESTINO, json=payload)

    if respuesta.status_code in [200, 201]:
        print(f" [v] Movimientos de {etiqueta} enviados correctamente.")
    else:
        print(
            f" [!] Error al enviar datos de {etiqueta}: "
            f"{respuesta.status_code} - {respuesta.text}"
        )

def cerrar_sesion_y_navegador(scraper):
    try:
        print(" [>] Cerrando sesión en Banco de Chile...")
        scraper.logout()
    except Exception as e:
        print(f" [!] No se pudo ejecutar cierre de sesión: {e}")
    finally:
        scraper.close()

def procesar_mensaje(ch, method, properties, body):
    print(" [x] Mensaje recibido de la cola.")
    scraper = None
    
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
            print(" [>] Iniciando extracción de movimientos de tarjeta de crédito...")
            movimientos_tc = scraper.extract_credit_card_transactions()
            
            # 4. Preparar payloads y cerrar sesión antes de enviar datos al backend
            payload = {
                "rut": rut,
                "movimientos": movimientos,
                "sourceType": "BANCO_CHILE_CC"
            }

            payload_tc = None

            if movimientos_tc:
                payload_tc = {
                    "rut": rut,
                    "movimientos": movimientos_tc,
                    "interface": "credit_card_v1",
                    "sourceType": "BANCO_CHILE_TC"
                }
            else:
                print(" [!] No se encontraron movimientos de tarjeta para enviar.")

            # Cierra el navegador antes de enviar la información.
            cerrar_sesion_y_navegador(scraper)
            scraper = None

            enviar_payload_backend(payload, 'cuenta', len(movimientos))

            if payload_tc is not None:
                enviar_payload_backend(payload_tc, 'tarjeta', len(movimientos_tc))
            
            # 5. Confirmar a RabbitMQ que el mensaje fue procesado exitosamente (Acknowledgment)
            ch.basic_ack(delivery_tag=method.delivery_tag)
            print(" [x] Proceso finalizado y mensaje removido de la cola.\n")
            
    except Exception as e:
        print(f" [X] Error durante el procesamiento: {e}")
        # En caso de error crítico, rechazamos el mensaje para que vuelva a la cola o se descarte
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        if scraper is not None:
            try:
                scraper.close()
            except Exception:
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