"""Rebuild the public feature graph. Optional documentation tool, not robot runtime."""
from collections import Counter
import html
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'graphify-out'
EXCLUDES = ['**/.local-review/**', '**/tests/**', 'test/**', '**/upstream/**',
            '**/assets/**', 'graphify-out/**', '.github/**',
            'Baymax-main/realtime_gemini_8.py', 'Baymax-main/realtime_gemini_9.py',
            'Baymax-main/realtime_gemini_9_stat_test.py', 'Baymax-main/test_speaker_devices.py',
            'devops/opi4-flash/test_*.py', 'devops/graphify/test_*.py', 'devops/performance/test_*.py']


def feature_html(text):
    # shortcut: render the feature log's headings/tables/lists only; use a Markdown
    # library if this documentation starts depending on the full Markdown grammar.
    def inline(value):
        escaped = re.sub(r'`([^`]+)`', r'<code>\1</code>', html.escape(value))
        return re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', escaped)
    blocks, table, paragraph = [], [], []
    def flush_paragraph():
        if paragraph:
            blocks.append('<p>' + inline(' '.join(paragraph)) + '</p>')
            paragraph.clear()
    def flush():
        if not table:
            return
        rows = [[cell.strip() for cell in line.strip('|').split('|')] for line in table]
        rows = [row for row in rows if not all(re.fullmatch(r'[: -]+', c) for c in row)]
        blocks.append('<div class="table-scroll"><table><thead><tr>' +
                      ''.join('<th>' + inline(c) + '</th>' for c in rows[0]) +
                      '</tr></thead><tbody>' + ''.join('<tr>' + ''.join('<td>' + inline(c) +
                      '</td>' for c in row) + '</tr>' for row in rows[1:]) + '</tbody></table></div>')
        table.clear()
    for line in text.splitlines():
        if line.startswith('|'):
            flush_paragraph()
            table.append(line)
            continue
        flush()
        if line.startswith('#'):
            flush_paragraph()
            level = min(3, len(line) - len(line.lstrip('#')))
            blocks.append(f'<h{level}>' + inline(line.lstrip('# ').strip()) + f'</h{level}>')
        elif line.strip():
            if line.startswith('- '):
                flush_paragraph()
                blocks.append('<ul><li>' + inline(line[2:]) + '</li></ul>')
            else:
                paragraph.append(line)
        else:
            flush_paragraph()
    flush()
    flush_paragraph()
    style = 'body{font:15px/1.6 system-ui,sans-serif;color:#202420;background:#fafbf9;margin:auto;max-width:1250px;padding:28px}h1{font-size:28px}h2{margin-top:32px}a{color:#28533b}table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;vertical-align:top;border-bottom:1px solid #ccd3cc;padding:10px}code{overflow-wrap:anywhere}.table-scroll{overflow-x:auto}th{white-space:nowrap}p{max-width:85ch}'
    return '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>OPi4 V1 feature log</title><style>' + style + '</style><body><nav><a href="graph.html">Interactive graph</a> · <a href="GRAPH_REPORT.md">Graph report</a></nav>' + ''.join(blocks) + '</body></html>'


def source_group(path):
    for fragment, label in [('/conversational_ability/', 'Conversation and Audio'),
                            ('/toolkits/everyday/', 'Everyday Support'),
                            ('/toolkits/family/', 'Family Connections'),
                            ('/toolkits/general_media_entertainment/', 'Media and Activities'),
                            ('/toolkits/', 'Callable Tools'), ('/eyes/', 'Physical Eyes'),
                            ('/pose/', 'Pose and Inference'), ('/opi4-flash/', 'SD Card Builder'),
                            ('docs/features/', 'Feature Documentation'), ('/deploy/', 'Installation and Updates')]:
        if fragment in '/' + path:
            return label
    return 'Runtime and Setup'


def portable_diagnostic(value):
    if isinstance(value, dict):
        return {key: portable_diagnostic(item) for key, item in value.items()}
    if isinstance(value, list):
        return [portable_diagnostic(item) for item in value]
    if isinstance(value, str) and Path(value).is_absolute():
        path = Path(value)
        try:
            return path.relative_to(ROOT).as_posix()
        except ValueError:
            return 'external-package/' + path.name
    return value


def main():
    from graphify.detect import detect, save_manifest
    from graphify.extract import extract
    from graphify.build import build_from_json
    from graphify.cluster import cluster, score_all
    from graphify.analyze import god_nodes, surprising_connections, suggest_questions
    from graphify.report import generate
    from graphify.export import to_json
    from graphify.diagnostics import diagnose_extraction, format_diagnostic_report
    from graphify.cli import _stamped_manifest_files
    OUT.mkdir(exist_ok=True)
    detection = detect(ROOT, extra_excludes=EXCLUDES, gitignore=True)
    if detection.get('skipped_sensitive'):
        print('Skipped sensitive files:', detection['skipped_sensitive'])
    if detection['total_files'] > 500 or detection['total_words'] > 2_000_000:
        raise SystemExit('Corpus exceeds the reviewed scope; narrow EXCLUDES before rebuilding.')
    structural = extract([Path(p) for p in detection['files']['code']], cache_root=ROOT, root=ROOT)
    semantic = json.loads((ROOT / 'docs/features/semantic_graph.json').read_text(encoding='utf-8'))
    ids = {n['id'] for n in structural['nodes']}
    nodes = structural['nodes'] + [n for n in semantic['nodes'] if n['id'] not in ids]
    extraction = dict(nodes=nodes, edges=structural['edges'] + semantic['edges'],
                      hyperedges=semantic.get('hyperedges', []))
    for item in extraction['nodes'] + extraction['edges']:
        source = item.get('source_file')
        if source:
            path = Path(source)
            relative = path.relative_to(ROOT) if path.is_absolute() else path
            if '..' in relative.parts or not (ROOT / relative).is_file():
                raise ValueError('Invalid source reference: ' + source)
            item['source_file'] = relative.as_posix()
    raw_health = diagnose_extraction(extraction, directed=False, root=str(ROOT))
    node_ids = {node['id'] for node in extraction['nodes']}
    unresolved = [edge for edge in extraction['edges']
                  if edge.get('source') not in node_ids or edge.get('target') not in node_ids]
    (OUT / 'UNRESOLVED_REFERENCES.json').write_text(json.dumps({
        'scope': 'References outside the selected project graph, including standard-library imports; not proof of missing runtime packages.',
        'count': len(unresolved), 'edges': unresolved}, indent=2), encoding='utf-8')
    extraction['edges'] = [edge for edge in extraction['edges']
                           if edge.get('source') in node_ids and edge.get('target') in node_ids]
    health = diagnose_extraction(extraction, directed=False, root=str(ROOT))
    print(format_diagnostic_report(health))
    print('Unresolved/external references omitted from the project view:', len(unresolved))
    raw_health, health = portable_diagnostic(raw_health), portable_diagnostic(health)
    graph = build_from_json(extraction, root=str(ROOT), directed=False)
    if not graph.number_of_nodes():
        raise ValueError('Refusing an empty graph')
    if graph.number_of_nodes() > 5000:
        print('GRAPH WARNING: over 5,000 nodes; the HTML exporter may aggregate communities.')
    communities = cluster(graph)
    cohesion = score_all(graph, communities)
    labels = {cid: Counter(source_group(str(graph.nodes[node].get('source_file', '')))
                           for node in members).most_common(1)[0][0]
              for cid, members in communities.items()}
    gods, surprises = god_nodes(graph), surprising_connections(graph, communities)
    questions = suggest_questions(graph, communities, labels)
    if not to_json(graph, communities, str(OUT / 'graph.json'), community_labels=labels):
        raise ValueError('Graphify refused to shrink the existing graph; review scope before replacing it')
    report = generate(graph, communities, cohesion, labels, gods, surprises, detection,
                      {'input': 0, 'output': 0}, 'Pallas-Robotics/Baymax', suggested_questions=questions)
    report = re.sub(r'^- Token cost:.*$', '- Token usage: structural extraction uses no LLM; semantic usage was not reported by the host.', report, flags=re.MULTILINE)
    report = '[Organized OPi4 V1 feature log](../docs/features/FEATURE_LOG.md) · [Interactive graph](graph.html) · [Rendered feature log](FEATURE_LOG.html)\n\n' + report
    report += '\n## Scope and integrity\n\nThe graph is documentation, not a hardware certification. Local paths are repository-relative. ' + str(len(unresolved)) + ' unresolved/external references (including standard-library imports) are omitted from this project-only view and retained in `UNRESOLVED_REFERENCES.json`; these are not claimed to be missing runtime dependencies. Parallel relationships can collapse in the native undirected view; raw semantic evidence remains in `docs/features/semantic_graph.json`.\n\nProject-view health:\n\n```json\n' + json.dumps(health, indent=2, default=str) + '\n```\n\nRaw extraction health before scope filtering:\n\n```json\n' + json.dumps(raw_health, indent=2, default=str) + '\n```\n'
    (OUT / 'GRAPH_REPORT.md').write_text(report, encoding='utf-8')
    (OUT / '.graphify_labels.json').write_text(json.dumps({str(k): v for k, v in labels.items()}), encoding='utf-8')
    (OUT / '.graphify_analysis.json').write_text(json.dumps(dict(communities=communities, cohesion=cohesion, gods=gods, surprises=surprises, questions=questions)), encoding='utf-8')
    (OUT / '.graphify_extract.json').write_text(json.dumps(extraction), encoding='utf-8')
    stamped = _stamped_manifest_files(detection['files'], extraction, ROOT)
    save_manifest(stamped, root=str(ROOT), scan_corpus={p for files in detection['files'].values() for p in files})
    (OUT / 'cost.json').write_text(json.dumps({'structural_llm_tokens': 0, 'semantic_input_tokens': None,
                                              'semantic_output_tokens': None, 'note': 'Host semantic usage unavailable; no provider API key was used.'}, indent=2), encoding='utf-8')
    subprocess.run([sys.executable, '-m', 'graphify', 'export', 'html'], cwd=ROOT, check=True)
    log = (ROOT / 'docs/features/FEATURE_LOG.md').read_text(encoding='utf-8')
    (OUT / 'FEATURE_LOG.html').write_text(feature_html(log), encoding='utf-8')
    native = OUT / 'graph.html'
    link = '<a id="feature-log-link" href="FEATURE_LOG.html" style="position:fixed;bottom:18px;left:18px;z-index:9999;padding:9px 14px;background:#fff;color:#202420;border:1px solid #777;border-radius:6px;font:14px system-ui">Organized feature log</a>'
    native.write_text(native.read_text(encoding='utf-8').replace('</body>', link + '</body>'), encoding='utf-8')
    print(f'Graph: {graph.number_of_nodes()} nodes, {graph.number_of_edges()} edges, {len(communities)} communities')
    print('Cohesion:', {labels[k] + ' ' + str(k): round(v, 3) for k, v in cohesion.items()})


if __name__ == '__main__':
    main()
