"""Render a DOCX document to a small, sanitized HTML fragment for admin preview.

Only text, headings, lists, bold/italic runs and tables are emitted. All text is
escaped; no styles, scripts, images or external relationships from the file are
copied into the output.
"""
import html
import zipfile
from xml.etree import ElementTree

_W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
_MAX_HTML = 400_000
_HEADING = {'Title': 'h1', 'Heading1': 'h2', 'Heading2': 'h3', 'Heading3': 'h4'}


def _text_of(node) -> str:
    parts = []
    for item in node.iter():
        if item.tag == _W + 't' and item.text:
            parts.append(item.text)
        elif item.tag == _W + 'tab':
            parts.append(' ')
        elif item.tag in {_W + 'br', _W + 'cr'}:
            parts.append('\n')
    return ''.join(parts)


def _run_html(run) -> str:
    text = _text_of(run)
    if not text:
        return ''
    escaped = html.escape(text).replace('\n', '<br>')
    props = run.find(_W + 'rPr')
    bold = props is not None and props.find(_W + 'b') is not None
    italic = props is not None and props.find(_W + 'i') is not None
    if bold:
        escaped = f'<strong>{escaped}</strong>'
    if italic:
        escaped = f'<em>{escaped}</em>'
    return escaped


def _paragraph_html(paragraph) -> str:
    props = paragraph.find(_W + 'pPr')
    style = ''
    align = ''
    if props is not None:
        style_node = props.find(_W + 'pStyle')
        if style_node is not None:
            style = style_node.get(_W + 'val') or ''
        align_node = props.find(_W + 'jc')
        if align_node is not None and align_node.get(_W + 'val') in {'center', 'right'}:
            align = align_node.get(_W + 'val')

    runs = ''.join(_run_html(run) for run in paragraph.findall(_W + 'r'))
    text = _text_of(paragraph)
    if not runs and text:
        runs = html.escape(text).replace('\n', '<br>')
    if not runs.strip():
        return ''

    tag = _HEADING.get(style, 'p')
    alignment = f' style="text-align:{align}"' if align else ''
    if props is not None and props.find(_W + 'numPr') is not None:
        return f'<li>{runs}</li>'
    return f'<{tag}{alignment}>{runs}</{tag}>'


def _table_html(table) -> str:
    rows = []
    for row in table.findall(_W + 'tr'):
        cells = []
        for cell in row.findall(_W + 'tc'):
            paragraphs = ''.join(_paragraph_html(p) for p in cell.findall(_W + 'p'))
            cells.append(f'<td>{paragraphs}</td>')
        rows.append('<tr>' + ''.join(cells) + '</tr>')
    return '<table>' + ''.join(rows) + '</table>' if rows else ''


def render_docx_html(data: bytes) -> str:
    with zipfile.ZipFile(__import__('io').BytesIO(data)) as archive:
        with archive.open('word/document.xml') as handle:
            root = ElementTree.parse(handle).getroot()
    body = root.find(_W + 'body')
    if body is None:
        return ''
    chunks = []
    list_open = False
    for child in body:
        if child.tag == _W + 'p':
            item = _paragraph_html(child)
            if item.startswith('<li>') and not list_open:
                chunks.append('<ul>')
                list_open = True
            elif not item.startswith('<li>') and list_open:
                chunks.append('</ul>')
                list_open = False
            if item:
                chunks.append(item)
        elif child.tag == _W + 'tbl':
            if list_open:
                chunks.append('</ul>')
                list_open = False
            item = _table_html(child)
            if item:
                chunks.append(item)
        if sum(len(chunk) for chunk in chunks) > _MAX_HTML:
            break
    if list_open:
        chunks.append('</ul>')
    return ''.join(chunks)[:_MAX_HTML]
