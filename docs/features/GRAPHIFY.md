# Graphify feature map

Start with [the organized feature log](FEATURE_LOG.md), then open the generated
[interactive graph](../../graphify-out/graph.html). Its **Organized feature log**
button opens the [rendered inventory](../../graphify-out/FEATURE_LOG.html).
GitHub displays HTML source; download the checkout and open these files locally,
or serve the repository with `python -m http.server` for browsing.

## Outputs and scope

- `graphify-out/graph.json`: source-linked structural/semantic knowledge graph.
- `graphify-out/graph.html`: native Graphify interactive viewer.
- `graphify-out/GRAPH_REPORT.md`: communities, cohesion, central nodes, integrity
  diagnostics and questions.
- `graphify-out/FEATURE_LOG.html`: organized board/feature/source/test inventory.
- `docs/features/semantic_graph.json`: reviewed document relationships, with
  source locations and explicit confidence.

The corpus covers public runtime, feature, setup and builder source/documents.
Tests, older runtime variants, upstream copies, assets, caches, recordings,
credentials, generated SD images and local review workspaces are excluded.
Presence in the graph means source/documentation exists, not that hardware passed.

## Rebuild

This is an optional developer tool. It adds nothing to the robot runtime and
opens no microphone, camera or provider session.

```bash
python -m pip install -r devops/graphify/requirements.txt
python devops/graphify/build.py
python -m unittest discover -s devops/graphify -p 'test_*.py'
```

The builder extracts code structurally with Graphify and reuses the checked-in
semantic document snapshot; no provider key is required. When docs change, refresh
that semantic snapshot with Graphify before rebuilding. Document source paths and
edge confidence must remain valid. Unknown host token counts are recorded as
unknown, not zero. Internal caches/interpreter paths are ignored by Git.

For installed Graphify tooling, from the repository root:

```bash
graphify query "conversation timing"
graphify query "Orange Pi board support"
```

The native undirected graph can collapse parallel relationships. Read the report's
integrity counts and the semantic evidence before making dependency claims.
References without a selected project node (including standard-library imports)
are kept in `graphify-out/UNRESOLVED_REFERENCES.json` and omitted from the project
view. An omitted reference does not mean that a runtime package is unavailable.
