#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
oc_extract_console.py — Lector a consola de Órdenes de Compra (ERP Maxxa)

BASE: versión 05 estable. Cambios Tanda A:
- Normalización Unicode (NFKC) + remoción de ZWSP/NBSP/FEFF/WORD JOINER.
- Folio robusto (Nº/N°/No/N o, con o sin ':').
- Condición de Compra multilínea con cortes por etiquetas conocidas; limpieza de '%'.
- Sanity check: unidad vacía y cuadratura qty*pu vs total (±1 CLP).
- Impresión limpia (unidad vacía sin “-”).

Uso:
  python3 oc_extract_console.py --pdf "/ruta/OC1012.pdf"
  python3 oc_extract_console.py --pdf-dir "/ruta/OCs"
  python3 oc_extract_console.py --pdf "/ruta/OC1026.pdf" --debug
"""

import argparse, os, re, sys, unicodedata
from pathlib import Path
from typing import List, Dict, Optional, Tuple

import hashlib

import pdfplumber

# ---------------- Configuración ----------------

UNITS_WHITELIST = {
    # listado provisto por ti (case-insensitive)
    "SC","PULG","UNID","CJ","DSP","SERV","BLS","DIA","TON","BD","CC","DOC","SET","TM","BL",
    "RO","TI","MM","SEM","1/2G","LT","KG","GR","CM","MTS","ANUA","MES","M2","M3","HRS","PAR",
    "GLN","TN","L","KM","1/8G","1/4G","GL","UF","PIE","MIN","MR","MI","G","CJ.","U","UN","UND"
}
UNITS_WHITELIST = {u.upper() for u in UNITS_WHITELIST}

FOOTER_STOP = re.compile(r"(?:^|\s)(Neto:|IVA\s*\(|Total:|Favor\s+Facturar\s+a:?)", re.I)
HEADER_LINE = re.compile(r"(?i)\bSKU\b.*\bDetalle\b.*\bCant\.\b.*\bUni\.\b.*\bNeto\b.*\bTotal\b")

KNOWN_LABELS_COLON_RE = re.compile(
    r"(Estado\s+del\s+Documento|Centro\s+de\s+Costo|Fecha\s+Emisi[oó]n|V[aá]lido\s+Hasta|Moneda|Condici[oó]n\s+de\s+Compra)\s*:",
    re.IGNORECASE,
)

WARN_IF_UNIT_EMPTY = True
TOLERANCIA_WARNING_CLP = 1

# ---------------- Utilidades ----------------

def normalize_text(s: Optional[str]) -> str:
    """NFKC + elimina NBSP/ZWSP/WORD JOINER/FEFF + colapsa espacios."""
    s = (s or "")
    # Normaliza forma canónica compuesta (homogeneiza números/letras y signos)
    s = unicodedata.normalize("NFKC", s)
    # Invisibles/espacios especiales
    s = (s
         .replace("\u00A0", " ")   # NBSP
         .replace("\u200B", "")    # ZWSP
         .replace("\u200C", "")    # ZWNJ
         .replace("\u200D", "")    # ZWJ
         .replace("\u2060", "")    # WORD JOINER
         .replace("\ufeff", ""))   # BOM
    # Colapsa espacios
    s = re.sub(r"\s+", " ", s.strip())
    return s

def deletter_space(line: str) -> str:
    """Compacta líneas con letras separadas por espacios (p. ej., 'C o n d i c i ó n')."""
    if re.search(r'(?:\b\w\b\s+){5,}\b\w\b', line) or re.search(r'(?:\d\s+){4,}\d', line):
        return re.sub(r'(?<=\w)\s+(?=\w)', '', line)
    return line

def strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')

def token_is_number(tok: str) -> bool:
    if not re.search(r"\d", tok or ""):
        return False
    # 1234,56 | 1.234,56 | 1.234 | 1234.56 | 8
    return bool(re.fullmatch(r"\d+(?:[.,]\d+)?|\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d{1,3}(?:,\d{3})+(?:\.\d+)?", tok))

def token_percent_value(tok: str) -> Optional[int]:
    if tok.endswith("%") and tok[:-1].isdigit():
        v = int(tok[:-1]);  return v if 0 <= v <= 99 else None
    if tok.isdigit():
        v = int(tok);       return v if 0 <= v <= 99 else None
    return None

def parse_number_latam(s: Optional[str]) -> Optional[float]:
    if not s: return None
    s = s.replace(" ", "")
    # dot+comma => latino
    if "." in s and "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s and "." not in s:
        s = s.replace(",", ".")
    elif "." in s and "," not in s:
        if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", s):
            s = s.replace(".", "")
    try:
        return float(s)
    except Exception:
        return None

UNIT_ALIASES = {"CJ.": "CJ", "UN": "UNID", "UND":"UNID", "U":"UNID", "MTS":"MTS"}
def looks_like_unit(tok: str) -> bool:
    if not tok: return False
    t = tok.upper().rstrip(".")
    t = UNIT_ALIASES.get(t, t)
    return (t in UNITS_WHITELIST) or (t in {"1/2G","1/4G"})

# ---------------- Texto → líneas y ventanas ----------------

def extract_lines(full_text: str) -> List[str]:
    raw_lines = (full_text or "").splitlines()
    # Primero compactar letras separadas y luego normalizar/limpiar invisibles
    return [normalize_text(deletter_space(l)) for l in raw_lines]

HEADER_KEYS = ("SKU","DETALLE","CANT","UNI","NETO","TOTAL","DESC")

def _header_hits(s: str) -> int:
    t = strip_accents(s or "").upper().replace(".", "")
    return sum(1 for k in HEADER_KEYS if k in t)

def find_table_header_last_idx(lines: List[str]) -> Optional[int]:
    U = [strip_accents(l).upper().replace(".", "") for l in lines]
    n = len(U)
    for i in range(n):
        # 1 línea
        if _header_hits(U[i]) >= 3 and ("SKU" in U[i]) and ("DETALLE" in U[i]):
            return i
        # 2 líneas
        if i+1 < n:
            combo2 = f"{U[i]} {U[i+1]}"
            if _header_hits(combo2) >= 3 and ("SKU" in combo2) and ("DETALLE" in combo2):
                return i+1
        # 3 líneas
        if i+2 < n:
            combo3 = f"{U[i]} {U[i+1]} {U[i+2]}"
            if _header_hits(combo3) >= 3 and ("SKU" in combo3) and ("DETALLE" in combo3):
                return i+2
    return None

def find_otrodatos_window(lines: List[str]) -> Tuple[int,int]:
    start = 0
    for i, ln in enumerate(lines):
        if re.search(r"(?i)\bOTROS\s+DATOS\b", ln):
            start = i + 1
            break
    end = len(lines)
    hdr_last = find_table_header_last_idx(lines[start:])
    if hdr_last is not None:
        end = start + hdr_last
    return start, end

# ---------------- PROVEEDOR (captura geométrica en el bloque derecho) ----------------

RUT_RE = re.compile(r"\b\d{7,8}[-–—][\dkK]\b")

def _extract_words(page):
    # Palabras con tolerancias suaves para reconstruir líneas
    return page.extract_words(x_tolerance=2, y_tolerance=3, use_text_flow=True)

def _find_anchor_proveedor(page):
    for w in _extract_words(page):
        if strip_accents(w["text"]).upper() == "PROVEEDOR":
            return w
    return None

def _table_header_top_y(page):
    tops = []
    for w in _extract_words(page):
        t = strip_accents(w["text"]).upper().replace(".", "")
        if t in {"SKU", "DETALLE", "CANT", "UNI", "NETO", "TOTAL"}:
            tops.append(w["top"])
    return min(tops) if tops else None

def _provider_roi(page):
    # Mitad derecha de la página, desde bajo "PROVEEDOR" hasta la cabecera de ítems
    anc = _find_anchor_proveedor(page)
    x_mid = page.width * 0.48
    x0 = x_mid
    y0 = (anc["bottom"] + 2) if anc else page.height * 0.28
    y1 = _table_header_top_y(page) or (page.height * 0.82)
    x1 = page.width - 6
    return (x0, y0, x1, y1)

def _group_lines(words, y_tol=3):
    words_sorted = sorted(words, key=lambda w: (w["top"], w["x0"]))
    lines, curr, base_y = [], [], None

    def flush():
        if not curr:
            return
        text = " ".join([w["text"] for w in sorted(curr, key=lambda ww: ww["x0"])])
        lines.append({
            "text": normalize_text(deletter_space(text)),
            "top": min(w["top"] for w in curr),
            "bottom": max(w["bottom"] for w in curr)
        })

    for w in words_sorted:
        if not curr:
            curr, base_y = [w], w["top"]
        elif abs(w["top"] - base_y) <= y_tol:
            curr.append(w)
        else:
            flush()
            curr, base_y = [w], w["top"]
    flush()
    return lines

def _is_name_fragment(s: str) -> bool:
    t = normalize_text(s)
    if not t:
        return False
    if re.search(r"(?i)\b(Fono|Email)\b", t):
        return False
    if re.search(r"(?i)\bChile\b", t):
        return False
    # Evita líneas con números típicas de dirección
    if re.search(r"\d{2,}", t):
        return False
    # Debe venir en mayúsculas (no minúsculas latinas)
    if re.search(r"[a-záéíóúñ]", t):
        return False
    return True

def extract_proveedor_from_page(page, debug: bool=False) -> Dict[str, str]:
    x0, y0, x1, y1 = _provider_roi(page)
    words = [w for w in _extract_words(page)
             if (x0 <= w["x0"] <= x1) and (x0 <= w["x1"] <= x1)
             and (y0 <= w["top"]) and (w["bottom"] <= y1)]
    lines = _group_lines(words)
    if debug:
        print(f"[DEBUG][PROV] ROI=({x0:.1f},{y0:.1f},{x1:.1f},{y1:.1f}); {len(lines)} líneas.")

    # 1) Primer RUT dentro del ROI
    rut_i, rut_val = None, None
    for i, ln in enumerate(lines):
        m = RUT_RE.search(ln["text"])
        if m:
            rut_i, rut_val = i, m.group(0)
            break
    if rut_i is None:
        return {}

    # 2) Nombre: parte izquierda de la línea del RUT + líneas superiores tipo nombre
    ln_text = lines[rut_i]["text"]
    mpos = RUT_RE.search(ln_text)
    left = normalize_text(ln_text[:mpos.start()])
    left = re.sub(r"[-–—]\s*$", "", left).strip()  # limpia guion terminal si quedó

    name_parts = []
    for j in range(rut_i - 1, max(rut_i - 4, -1), -1):  # mira hasta 3 líneas arriba
        tx = lines[j]["text"]
        if _is_name_fragment(tx):
            name_parts.insert(0, tx)
        else:
            break
    if left:
        name_parts.append(left)

    nombre = normalize_text(" ".join(name_parts))
    out = {"proveedor_rut": rut_val}
    if nombre:
        out["proveedor_nombre"] = nombre
    if debug:
        print(f"[DEBUG][PROV] rut={rut_val} ; nombre='{nombre}'")
    return out

# ---------------- Encabezado ----------------

def parse_header_from_text(full_text: str) -> dict:
    """Campos generales (no incluye 'condicion_compra' ni 'descripcion_cotizacion')."""
    header: Dict[str, str] = {}
    # Trabajar sobre texto normalizado por líneas
    lines = extract_lines(full_text)
    top_slice = "\n".join(lines[:15])  # primeras líneas p.1 donde viene el Folio

    # Folio: acepta Nº/N°/No/N o con o sin ':'
    m_folio = re.search(r"Folio\s*(?:N[º°o]|No|Nº|N°)?\s*[:#]?\s*(\d+)", top_slice, flags=re.I)
    if m_folio: header["oc_numero"] = m_folio.group(1)

    block = "\n".join(lines)  # normalizado

    # Fechas / Estado / Centro / Moneda
    m_estado = re.search(r"Estado del Documento\s*:?\s*([^\n\r]+?)(?=\s*(Centro de Costo|Fecha Emisi[oó]n|Moneda|Condici[oó]n de Compra|$))", block, flags=re.I)
    if m_estado: header["estado_documento"] = normalize_text(m_estado.group(1))

    m_fecha = re.search(r"Fecha\s*Emisi[oó]n\s*:\s*(\d{2}-\d{2}-\d{4})", block)
    if m_fecha: header["fecha_emision"] = m_fecha.group(1)

    m_costo = re.search(r"Centro de Costo\s*:?\s*([^\n\r]+?)(?=\s*(Moneda|Condici[oó]n de Compra|Fecha Emisi[oó]n|Estado del Documento|$))", block, flags=re.I)
    if m_costo: header["centro_costo"] = normalize_text(m_costo.group(1))

    m_moneda = re.search(r"Moneda\s*:\s*([A-Z]{2,4})", block)
    if m_moneda: header["moneda"] = m_moneda.group(1)

    return header

def extract_condicion_y_descripcion(full_text: str) -> Dict[str,str]:
    """
    Dentro de 'OTROS DATOS':
      - 'condicion_compra': tras 'Condición de Compra:' en la misma línea; si continúa,
        concatena la 'cola' de la línea siguiente DESPUÉS de 'Moneda: <CLP|USD|EUR|UF>'.
      - 'descripcion_oc': líneas no vacías POSTERIORES a la línea de 'Moneda:' hasta cabecera tabla.
    """
    out: Dict[str,str] = {}
    lines = extract_lines(full_text)
    start, end = find_otrodatos_window(lines)
    window = lines[start:end]

    # Buscar línea con 'Condición de Compra:'
    for i, ln in enumerate(window):
        m = re.search(r"(?i)Condici[oó]n\s*de\s*Compra\s*:\s*(.*)", ln)
        if not m:
            continue
        part1 = normalize_text(m.group(1) or "")
        cut = KNOWN_LABELS_COLON_RE.search(part1)
        if cut: part1 = normalize_text(part1[:cut.start()])

        # posible cola en la línea siguiente si empieza con 'Moneda: <código>'
        cond = part1
        moneda_line_idx = None
        if i + 1 < len(window):
            ln2 = window[i+1]
            m2 = re.search(r"(?i)Moneda\s*:\s*(CLP|USD|EUR|UF)\b(.*)", ln2)
            if m2:
                moneda_line_idx = i + 1
                tail = normalize_text(m2.group(2) or "")
                cut2 = KNOWN_LABELS_COLON_RE.search(tail)
                if cut2: tail = normalize_text(tail[:cut2.start()])
                if tail:
                    cond = normalize_text(f"{part1} {tail}")

        # estética: espacios alrededor de %
        cond = re.sub(r"\s*%\s*", "% ", cond)
        out["condicion_compra"] = cond

        # Si no detecté 'Moneda:' en la línea siguiente, localizo su posición en ventana
        if moneda_line_idx is None:
            for j in range(i, len(window)):
                if re.search(r"(?i)^Moneda\s*:\s*(CLP|USD|EUR|UF)\b", window[j]):
                    moneda_line_idx = j
                    break

        # Descripción de OC: líneas posteriores a 'Moneda:'
        desc = ""
        if moneda_line_idx is not None:
            desc_lines = [lnn for lnn in window[moneda_line_idx+1:] if lnn.strip()]
            desc = " ".join(desc_lines).strip()
        out["descripcion_oc"] = desc
        break

    # Si no encontré 'Condición de Compra', al menos intenta armar descripción tras 'Moneda:'
    if "descripcion_oc" not in out:
        mon_idx = None
        for i, ln in enumerate(window):
            if re.search(r"(?i)^Moneda\s*:\s*(CLP|USD|EUR|UF)\b", ln):
                mon_idx = i; break
        if mon_idx is not None:
            desc_lines = [lnn for lnn in window[mon_idx+1:] if lnn.strip()]
            out["descripcion_oc"] = " ".join(desc_lines).strip()
    return out

# ---------------- Ítems ----------------

def parse_item_line_tail(line: str) -> Optional[Dict]:
    toks = line.split()
    if not toks:
        return None
    # find numeric token indices
    num_idxs = [i for i,t in enumerate(toks) if token_is_number(t)]
    if not num_idxs:
        return None
    total_idx = num_idxs[-1]
    total = parse_number_latam(toks[total_idx])

    # percent candidate
    pct_idx = None
    pct_val = None
    if len([i for i in num_idxs if i < total_idx]) >= 2:
        cand_idx = total_idx - 1
        if token_is_number(toks[cand_idx]) and (cand_idx - 1) >= 0 and token_is_number(toks[cand_idx - 1]):
            v = token_percent_value(toks[cand_idx])
            if v is not None:
                pct_idx = cand_idx
                pct_val = v

    pu_idx = (pct_idx - 1) if pct_idx is not None else (total_idx - 1)
    if pu_idx < 0 or not token_is_number(toks[pu_idx]):
        return None
    pu = parse_number_latam(toks[pu_idx])

    unit_idx = pu_idx - 1
    unit = None
    if unit_idx >= 0 and looks_like_unit(toks[unit_idx]):
        unit = UNIT_ALIASES.get(toks[unit_idx].upper().rstrip("."), toks[unit_idx].upper().rstrip("."))
        qty_idx = unit_idx - 1
    else:
        qty_idx = pu_idx - 1

    if qty_idx < 0 or not token_is_number(toks[qty_idx]):
        return None
    qty = parse_number_latam(toks[qty_idx])

    # code & description
    first = toks[0]
    if first == '-' or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9\-_/\.]*', first):
        codigo = first
        descripcion = " ".join(toks[1:qty_idx]).strip()
    else:
        codigo = "-"
        descripcion = " ".join(toks[:qty_idx]).strip()

    return {
        "codigo": codigo if codigo else "-",
        "descripcion": normalize_text(descripcion),
        "unidad": unit,
        "cantidad": qty,
        "precio_unitario": pu,
        "pct_desc": pct_val,
        "total_linea": total,
    }

def parse_items_from_pages(pdf_path: Path, debug: bool=False) -> List[Dict]:
    """
    Lee la tabla de ítems atravesando TODAS las páginas.
    """
    items: List[Dict] = []
    linea_idx = 0
    in_table = False

    with pdfplumber.open(str(pdf_path)) as pdf:
        for page_no, page in enumerate(pdf.pages, 1):
            raw = page.extract_text() or ""
            lines = extract_lines(raw)

            header_end = find_table_header_last_idx(lines)
            if header_end is not None:
                in_table = True
                body_iter = lines[header_end+1:]
            else:
                if not in_table:
                    continue
                body_iter = lines

            for ln_i, ln in enumerate(body_iter, 1):
                if FOOTER_STOP.search(ln):
                    in_table = False
                    break
                if not ln.strip():
                    continue

                got = parse_item_line_tail(ln)
                if got:
                    linea_idx += 1
                    got["linea"] = linea_idx
                    if debug:
                        print(f"[DEBUG][ITEM] p{page_no:01d} l{ln_i:03d}: {ln}")
                        print("  ->", got)
                    items.append(got)
                else:
                    if items and not re.search(r"^(SKU\b|NETO:|TOTAL:|IVA\b|FAVOR\s+FACTURAR\s+A:)", ln, re.I):
                        if debug:
                            print(f"[DEBUG][CONT] p{page_no:01d} l{ln_i:03d}: {ln!r} \u2192 desc += \u2026")
                        items[-1]["descripcion"] = normalize_text(items[-1]["descripcion"] + " " + ln)

    return items

# ---------------- Enriquecer ítems ----------------

def enrich_items(items: List[Dict], oc_num: str) -> List[Dict]:
    """
    Agrega:
      - precio_unitario_efectivo = total_linea / cantidad (si aplica)
      - clave_item = hash SHA1 corto de (oc_num|linea|codigo|descripcion_normalizada)
    """
    for it in items:
        q   = it.get("cantidad")
        tot = it.get("total_linea")
        it["precio_unitario_efectivo"] = round(tot / q, 6) if (q and tot is not None and q != 0) else None

        base = f"{oc_num}|{it.get('linea')}|{(it.get('codigo') or '-').strip()}|{normalize_text(it.get('descripcion') or '')}"
        it["clave_item"] = hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]
    return items

# ---------------- Neto, Iva, Total por OC ----------------

NUM_RE = r"([\d\.\,]+)"  # acepta 1.234,56 ; 1.234 ; 1234,56 ; 1234

def extract_totales(full_text: str) -> dict:
    """
    Busca 'Neto:', 'IVA(XX%):' y 'Total:' en todo el texto normalizado.
    Devuelve {'neto_oc': float, 'iva_oc': float, 'total_oc': float} si los encuentra.
    """
    txt = normalize_text(full_text)
    out = {}

    m_neto = re.search(r"(?i)\bNeto\s*:\s*" + NUM_RE, txt)
    if m_neto:
        out["neto_oc"] = parse_number_latam(m_neto.group(1))

    m_iva = re.search(r"(?i)\bIVA\s*\(\s*\d{1,2}\s*%?\s*\)\s*:\s*" + NUM_RE, txt)
    if m_iva:
        out["iva_oc"] = parse_number_latam(m_iva.group(1))

    m_total = re.search(r"(?i)\bTotal\s*:\s*" + NUM_RE, txt)
    if m_total:
        out["total_oc"] = parse_number_latam(m_total.group(1))

    return out

# ---------------- Sanity / Orquestación ----------------

def sanity_warnings(items: List[Dict]) -> List[str]:
    warns = []
    for i, it in enumerate(items, start=1):
        # 1) Unidad vacía: opcional
        uni = (it.get("unidad") or "").strip()
        if WARN_IF_UNIT_EMPTY and not uni:
            warns.append(f"[INFO] Ítem #{i:03d} sin unidad (codigo={it.get('codigo','-')}).")

        # 2) Cuadratura con (o sin) %desc
        q   = it.get("cantidad")
        pu  = it.get("precio_unitario")
        tot = it.get("total_linea")
        if None in (q, pu, tot):
            continue

        pct = it.get("pct_desc")
        if pct is not None:
            expected = round(q * pu * (1 - (pct / 100.0)))
        else:
            expected = round(q * pu)

        if abs(expected - round(tot)) > TOLERANCIA_WARNING_CLP:
            warns.append(
                f"[WARNING] Ítem #{i:03d} descuadra: esperado≈{expected} "
                f"vs total({tot:.2f}) [q={q}, pu={pu}, pct={pct}]"
            )
    return warns

def process_pdf(pdf_path: Path, debug: bool=False):
    with pdfplumber.open(str(pdf_path)) as pdf:
        pages = pdf.pages
        full_text = "\n".join([p.extract_text() or "" for p in pages])

    header = parse_header_from_text(full_text)
    extras = extract_condicion_y_descripcion(full_text)
    header.update(extras)
    # Totales del pie (neto/iva/total)
    header.update(extract_totales(full_text))
        # Proveedor (geométrico) solo desde p.1
    try:
        with pdfplumber.open(str(pdf_path)) as _pdf:
            geo = extract_proveedor_from_page(_pdf.pages[0], debug=debug)
        if geo.get("proveedor_rut"):
            header["proveedor_rut"] = geo["proveedor_rut"]
        if geo.get("proveedor_nombre"):
            header["proveedor_nombre"] = geo["proveedor_nombre"]
    except Exception as e:
        if debug:
            print(f"[DEBUG][PROV] error ROI proveedor: {e}")

    items = parse_items_from_pages(pdf_path, debug=debug)
    items = enrich_items(items, header.get("oc_numero", ""))
    return header, items

def print_result(pdf: Path, header: Dict[str,str], items: List[Dict]):
    print(f"\n===== Archivo: {pdf.name} =====")
    print("— ENCABEZADO —")
    orden = ["oc_numero","fecha_emision","moneda","estado_documento","centro_costo","condicion_compra","descripcion_oc", "proveedor_nombre", "proveedor_rut", "neto_oc", "iva_oc", "total_oc"]
    for k in orden:
        if k in header:
            print(f"{k}: {header[k]}")
    extras = [k for k in header.keys() if k not in orden]
    for k in extras:
        print(f"{k}: {header[k]}")

    print("\n— ÍTEMS —")
    if not items:
        print("(sin ítems detectados)")
        return
    for it in items:
        qty = it["cantidad"]; pu = it["precio_unitario"]; tot = it["total_linea"]; pct = it.get("pct_desc")
        unit = it.get("unidad") or ""
        cod  = it.get("codigo") or "-"
        base = f"[{it['linea']:03d}] codigo={cod}  unidad={unit}  qty={qty}  pu={pu}"
        if pct is not None:
            base += f"  pct={pct}"
        base += f"  total={tot}"
        print(base)
        print(f"      desc: {it['descripcion']}")

    # Sanity
    for w in sanity_warnings(items):
        print(w)

# ---------------- CLI ----------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", help="Ruta al PDF de OC")
    ap.add_argument("--pdf-dir", help="Carpeta con PDFs")
    ap.add_argument("--debug", action="store_true", help="Trazas de detección (ítems)")
    args = ap.parse_args()

    if not args.pdf and not args.pdf_dir:
        print("Debe especificar --pdf o --pdf-dir"); sys.exit(1)

    if args.pdf:
        pdfs = [Path(os.path.expanduser(args.pdf))]
    else:
        base = Path(os.path.expanduser(args.pdf_dir))
        pdfs = sorted(base.glob("*.pdf"))

    for pdf in pdfs:
        try:
            header, items = process_pdf(pdf, debug=args.debug)
            print_result(pdf, header, items)
        except Exception as e:
            print(f"[ERROR] {pdf.name}: {e}")

if __name__ == "__main__":
    main()

def parse_pdf_file(pdf_path: str):
    """
    Devuelve (header, items) para un PDF.
    Ajusta 'process_pdf' si tu función principal tiene otro nombre.
    """
    res = process_pdf(pdf_path, debug=False)
    if isinstance(res, dict):
        header = res.get("header", {})
        items = res.get("items", [])
    else:
        header, items = res
    return header, items
