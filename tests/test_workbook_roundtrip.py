"""Real editor-compatible empty inline-string cells in follow-up sheets."""
from pathlib import Path
import sys
import tempfile
import unittest
from zipfile import ZipFile,ZIP_DEFLATED
import xml.etree.ElementTree as ET
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import xlsx_writer as xw


class WorkbookRoundtripTests(unittest.TestCase):
    def test_empty_inline_string_without_is_node_is_a_blank_note(self):
        with tempfile.TemporaryDirectory() as folder:
            file=Path(folder)/'notes.xlsx'
            xw.write_xlsx(file,[('人工跟进',[['岗位唯一键','人工投递状态','人工优先级','人工备注'],['job-one','','','']])])
            with ZipFile(file) as z:parts={name:z.read(name) for name in z.namelist()}
            sheet=ET.fromstring(parts['xl/worksheets/sheet1.xml'])
            for row in sheet.findall('{*}sheetData/{*}row')[1:]:
                for cell in row:
                    if cell.get('r','').startswith(('B','C','D')):
                        for child in list(cell):cell.remove(child)
                        cell.set('t','inlineStr')
            parts['xl/worksheets/sheet1.xml']=ET.tostring(sheet,encoding='utf-8',xml_declaration=True)
            with ZipFile(file,'w',ZIP_DEFLATED) as z:
                for name,value in parts.items():z.writestr(name,value)
            self.assertEqual(xw.read_followup(file),[['job-one','','','']])
