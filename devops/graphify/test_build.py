import json
from pathlib import Path
import unittest

from build import ROOT, feature_html


class GraphDocumentationTests(unittest.TestCase):
    def test_feature_log_renders_tables_and_escapes_source_text(self):
        rendered = feature_html('# Log\n\n**Board**\ncompatibility\n\n| Feature | Source |\n|---|---|\n| Audio | `timing.py` |\n\n<script>bad()</script>')
        self.assertIn('<p><strong>Board</strong> compatibility</p>', rendered)
        self.assertIn('<table>', rendered)
        self.assertIn('<code>timing.py</code>', rendered)
        self.assertIn('&lt;script&gt;', rendered)
        self.assertNotIn('<script>bad()', rendered)

    def test_semantic_graph_has_valid_sources_and_endpoints(self):
        data = json.loads((ROOT / 'docs/features/semantic_graph.json').read_text(encoding='utf-8'))
        nodes = {node['id'] for node in data['nodes']}
        self.assertEqual(len(nodes), len(data['nodes']))
        for item in data['nodes'] + data['edges']:
            path = Path(item['source_file'])
            self.assertFalse(path.is_absolute())
            self.assertNotIn('..', path.parts)
            self.assertTrue((ROOT / path).is_file(), path)
        for edge in data['edges']:
            self.assertIn(edge['source'], nodes)
            self.assertIn(edge['target'], nodes)
            self.assertIn(edge['confidence'], ('EXTRACTED', 'INFERRED', 'AMBIGUOUS'))
            if edge['confidence'] == 'EXTRACTED':
                self.assertEqual(edge['confidence_score'], 1)

    def test_graph_and_feature_log_are_included_together(self):
        output = ROOT / 'graphify-out'
        self.assertTrue((output / 'graph.json').is_file())
        self.assertIn('feature-log-link', (output / 'graph.html').read_text(encoding='utf-8'))
        self.assertIn('OPi4 V1 feature log', (output / 'FEATURE_LOG.html').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
