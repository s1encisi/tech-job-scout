"""Public-template privacy and the standalone offline demonstration."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from zipfile import ZipFile
import xml.etree.ElementTree as ET

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / 'examples'))
import demo


class DemoTests(unittest.TestCase):
    def test_public_profile_contains_no_confirmed_personal_facts(self):
        profile = json.loads((BASE / 'templates/profile.json').read_text(encoding='utf-8'))
        for fact in profile['facts'].values():
            self.assertEqual(fact, {'value': None, 'status': 'unknown',
                                    'source': None, 'confirmed_at': None})

    def test_demo_exports_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'demo'
            with mock.patch('socket.socket', side_effect=AssertionError('Network forbidden')):
                summary = demo.run(output)
            self.assertEqual(summary['unique_jobs'], 1)
            self.assertEqual(summary['duplicate_ingest_new_jobs'], 0)
            self.assertEqual(summary['quarantined_observations'], 1)
            self.assertEqual(summary['run_status'], 'partial')
            workbook = output / 'exports/环境_AI_岗位追踪_latest.xlsx'
            with ZipFile(workbook) as archive:
                xml = ET.fromstring(archive.read('xl/workbook.xml'))
                sheets = xml.findall('{*}sheets/{*}sheet')
                self.assertEqual(len(sheets), 13)
                self.assertIn('人工跟进', [sheet.attrib['name'] for sheet in sheets])

    def test_demo_refuses_to_overwrite_existing_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'keep.txt'
            marker.write_text('preserve me', encoding='utf-8')
            with self.assertRaises(ValueError):
                demo.run(directory)
            self.assertEqual(marker.read_text(encoding='utf-8'), 'preserve me')
