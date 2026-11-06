[Organized OPi4 V1 feature log](../docs/features/FEATURE_LOG.md) · [Interactive graph](graph.html) · [Rendered feature log](FEATURE_LOG.html)

# Graph Report - Baymax  (2026-11-05)

## Corpus Check
- 197 files · ~175,252 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 47 file(s) not represented in the graph (top: .service 11, (none) 8, .cmd 7)

## Summary
- 1876 nodes · 3200 edges · 129 communities (72 shown, 57 thin omitted)
- Extraction: 97% EXTRACTED · 3% INFERRED · 0% AMBIGUOUS · INFERRED: 106 edges (avg confidence: 0.86)
- Token usage: structural extraction uses no LLM; semantic usage was not reported by the host.

## Community Hubs (Navigation)
- Callable Tools
- Physical Eyes
- Everyday Support
- Runtime and Setup
- Callable Tools
- Runtime and Setup
- Feature Documentation
- Runtime and Setup
- Conversation and Audio
- Pose and Inference
- SD Card Builder
- Pose and Inference
- Conversation and Audio
- Installation and Updates
- Conversation and Audio
- Pose and Inference
- Callable Tools
- Runtime and Setup
- Callable Tools
- Runtime and Setup
- Runtime and Setup
- Pose and Inference
- Runtime and Setup
- Callable Tools
- Pose and Inference
- Callable Tools
- Runtime and Setup
- Callable Tools
- Runtime and Setup
- Runtime and Setup
- Runtime and Setup
- Runtime and Setup
- Pose and Inference
- Callable Tools
- Conversation and Audio
- Runtime and Setup
- Runtime and Setup
- Callable Tools
- Runtime and Setup
- Conversation and Audio
- Callable Tools
- Family Connections
- Installation and Updates
- Runtime and Setup
- Runtime and Setup
- Family Connections
- Callable Tools
- Runtime and Setup
- Runtime and Setup
- Runtime and Setup
- Runtime and Setup
- Runtime and Setup
- Family Connections
- Callable Tools
- Runtime and Setup
- Runtime and Setup
- SD Card Builder
- Callable Tools
- Conversation and Audio
- Callable Tools
- Everyday Support
- Installation and Updates
- Pose and Inference
- Callable Tools
- Callable Tools
- Runtime and Setup
- Conversation and Audio
- Pose and Inference
- Pose and Inference
- Runtime and Setup
- Runtime and Setup
- Runtime and Setup
- Installation and Updates
- Pose and Inference
- Pose and Inference
- Runtime and Setup
- Runtime and Setup
- Pose and Inference
- Installation and Updates
- Installation and Updates
- Runtime and Setup
- Callable Tools
- Runtime and Setup
- Runtime and Setup
- SD Card Builder
- Installation and Updates
- Pose and Inference
- Runtime and Setup
- Runtime and Setup
- Installation and Updates
- Callable Tools
- Runtime and Setup
- Runtime and Setup
- Runtime and Setup
- Runtime and Setup
- Runtime and Setup
- Pose and Inference
- Installation and Updates
- Installation and Updates
- Installation and Updates
- Installation and Updates
- Installation and Updates
- Installation and Updates
- Installation and Updates
- Runtime and Setup
- Runtime and Setup
- Runtime and Setup
- Runtime and Setup
- Runtime and Setup
- Runtime and Setup
- Runtime and Setup
- Runtime and Setup
- Runtime and Setup
- Runtime and Setup

## God Nodes (most connected - your core abstractions)
1. `OPi4 V1 organized feature log` - 23 edges
2. `VoiceAnalyzer` - 22 edges
3. `Store` - 20 edges
4. `PublicSources` - 20 edges
5. `check()` - 19 edges
6. `run()` - 19 edges
7. `Player` - 18 edges
8. `RknnEngine` - 17 edges
9. `record()` - 17 edges
10. `InputEnhancer` - 16 edges

## Surprising Connections (you probably didn't know these)
- `main()` --calls--> `public_configuration()`  [INFERRED]
  devops/clickables/launch.py → Baymax-main/dev_settings.py
- `worker()` --calls--> `sample()`  [INFERRED]
  Baymax-main/runtime_monitor.py → devops/performance/check.py
- `main()` --calls--> `read_status()`  [INFERRED]
  devops/clickables/launch.py → Baymax-main/runtime_monitor.py
- `main()` --calls--> `public_configuration()`  [INFERRED]
  devops/opi4-flash/payload/ember-hardware.py → Baymax-main/dev_settings.py
- `main()` --calls--> `save_configuration()`  [INFERRED]
  devops/opi4-flash/payload/ember-hardware.py → Baymax-main/dev_settings.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Shared pose and fall-check-in flow** — baymax_main_features_toolkits_fall_detection_fall_toolkit, baymax_main_features_toolkits_fall_detection_shared_pose, baymax_main_features_pose_readme_vision_child, baymax_main_features_pose_readme_pose, baymax_main_features_toolkits_fall_detection_fall_checkin [EXTRACTED 1.00]
- **Family messages and everyday reminder sync** — baymax_main_features_toolkits_family_readme_family, baymax_main_features_toolkits_family_readme_app_sync, baymax_main_features_toolkits_family_readme_family_store, baymax_main_features_toolkits_everyday_readme_everyday, baymax_main_features_toolkits_family_readme_confirmed_replies [EXTRACTED 1.00]

## Communities (129 total, 57 thin omitted)

### Community 0 - "Callable Tools"
Cohesion: 0.05
Nodes (29): ConversationSources, discovery_enabled(), enabled_names(), extend_config(), handle_tool_call(), run_call(), Toolkit, Headlines (+21 more)

### Community 1 - "Physical Eyes"
Cohesion: 0.05
Nodes (20): attach(), AutoEyes, cue(), medical_alert(), reply_cue(), requested(), clear_failures(), expected_sensors() (+12 more)

### Community 2 - "Everyday Support"
Cohesion: 0.07
Nodes (19): _at(), _candidates(), _clock(), _days_in_month(), describe(), device_timezone(), _ints(), local_start() (+11 more)

### Community 3 - "Runtime and Setup"
Cohesion: 0.07
Nodes (38): _alert_recipients(), auth_login(), auth_logout(), auth_me(), auth_resend(), auth_signup(), auth_verify(), camera_stream() (+30 more)

### Community 4 - "Callable Tools"
Cohesion: 0.05
Nodes (54): Fall package requirements, Optional strict address gate, Whole-frame PCM processing, Conversation and turn-taking, Existing Gemini Live session, Context compression and session resumption, Local speech and latency estimates, WebRTC echo cancellation and cleanup (+46 more)

### Community 5 - "Runtime and Setup"
Cohesion: 0.07
Nodes (20): _build_transcript(), _collect_segments(), main(), _merge_segments(), _poll_jobs(), _report_failure(), _run_analysis(), _enqueue() (+12 more)

### Community 6 - "Feature Documentation"
Cohesion: 0.06
Nodes (51): Independent Ember and Monitor user services, MVP startup and update guide, Opt-in release updater with rollback, On-board device settings, PortAudio / PipeWire / WirePlumber audio packages, Compiler, Python, Git and network system packages, XFCE / LightDM / Xorg desktop packages, Image system package list (+43 more)

### Community 7 - "Runtime and Setup"
Cohesion: 0.07
Nodes (30): api_key(), _assigned(), card(), check(), core_session(), declared_tools(), disclosure(), image_file() (+22 more)

### Community 8 - "Conversation and Audio"
Cohesion: 0.06
Nodes (13): apply(), audio_ready(), enabled(), Engagement, heard(), _listener(), register(), download_model() (+5 more)

### Community 9 - "Pose and Inference"
Cohesion: 0.07
Nodes (21): _close_quietly(), create_pose_backend(), driver_hint(), _driver_hint(), _flat(), installed_rknnlite_version(), npu_unavailable_reason(), _open_npu() (+13 more)

### Community 10 - "SD Card Builder"
Cohesion: 0.11
Nodes (23): asset(), digest(), generate_credentials(), attach(), chroot_down(), chroot_up(), detach(), build_linux.sh script (+15 more)

### Community 11 - "Pose and Inference"
Cohesion: 0.12
Nodes (26): build_dir(), build_rknn(), check(), check_failures(), check_limits(), check_sequences(), compare_onnx_tflite(), compare_pipelines() (+18 more)

### Community 12 - "Conversation and Audio"
Cohesion: 0.10
Nodes (9): ResidualEcho, create(), enabled(), extend_config(), fan_filter_hz(), FanFilter, InputEnhancer, mute_during_reply() (+1 more)

### Community 13 - "Installation and Updates"
Cohesion: 0.13
Nodes (23): block_ssid(), client_mode(), hostapd_config(), hotspot_name(), join_saved(), log(), main(), network_block() (+15 more)

### Community 14 - "Conversation and Audio"
Cohesion: 0.09
Nodes (5): AddressGate, follow_up_seconds(), is_speech(), names_ember(), _WatchedSession

### Community 15 - "Pose and Inference"
Cohesion: 0.10
Nodes (11): import_rknnlite(), _rknn_engine(), _by_size(), _finite(), heatmap_hwc(), OnnxEngine, RknnEngine, rknnlite_class() (+3 more)

### Community 16 - "Callable Tools"
Cohesion: 0.10
Nodes (9): ensure_model(), EyeContact, FaceLandmarks, looking(), presence_enabled(), Proactive, _setting(), startup_enabled() (+1 more)

### Community 17 - "Runtime and Setup"
Cohesion: 0.11
Nodes (16): latest_frames_only(), _draw_angle_graph(), _draw_annotated(), _draw_fall_alert(), _draw_hud(), _draw_skeleton(), _draw_trunk_line(), _ensure_pose_model() (+8 more)

### Community 18 - "Callable Tools"
Cohesion: 0.11
Nodes (14): _clean_query(), _covers_query(), download(), fetch_book(), _ia_search(), _keep_only(), _lucene_terms(), _normalise_title() (+6 more)

### Community 19 - "Runtime and Setup"
Cohesion: 0.14
Nodes (24): AI core and webapp container composition, Experimental Docker deployment, Experimental core/webapp containers, Host boot and optional Wi-Fi hotspot tooling, Synthetic Gemini-in-the-loop evaluation, MediaPipe fall detection setup, Integrated v10 MVP installation, Orange Pi board-specific Debian setup (+16 more)

### Community 20 - "Runtime and Setup"
Cohesion: 0.15
Nodes (18): transcript_overlap(), _append_playback(), receive_audio(), play_reply_audio(), show_reply_text(), _retrieve_memories(), _transcript_add(), consume_stop_request() (+10 more)

### Community 21 - "Pose and Inference"
Cohesion: 0.15
Nodes (18): compare(), fall_detector(), _jsonable(), main(), pct(), _print_clip(), _print_summary(), _rate() (+10 more)

### Community 22 - "Runtime and Setup"
Cohesion: 0.12
Nodes (24): Editing and validation protocol, Draft PR and software-lead review before merge/deployment, Preserve user edits, import paths and data flows, Board-specific live and fresh-card validation, Orange Pi scope: Pallas-Robotics/Baymax focused branch, Keep credentials, recordings, models and private images out of Git and graphs, Graphify AST extraction plus reviewed semantic document snapshot, Graphify feature map guide (+16 more)

### Community 23 - "Callable Tools"
Cohesion: 0.13
Nodes (8): CheckIns, enabled(), _last_conversation(), _last_heard(), _path(), _setting(), window(), windows()

### Community 24 - "Pose and Inference"
Cohesion: 0.12
Nodes (9): cpu_factory(), default_mediapipe_options(), MediaPipeCpuBackend, frames_from(), main(), run(), summary(), Timed (+1 more)

### Community 27 - "Callable Tools"
Cohesion: 0.14
Nodes (11): enabled(), extend_config(), silent(), enabled(), extend_config(), handle_tool_call(), prewarm(), warm() (+3 more)

### Community 28 - "Runtime and Setup"
Cohesion: 0.10
Nodes (12): _abandon_reply(), _abort_connection(), fall_alert_monitor(), _flush_playback(), get_default_device_id(), listen_audio(), monitor_queues(), pin_pipewire_sink() (+4 more)

### Community 29 - "Runtime and Setup"
Cohesion: 0.15
Nodes (21): Every feature PR records off/on/combined performance evidence, Feature active/on comparison, Explicit added-p95 speech-end-to-playback budget, Aggregate JSON performance reports under .local-review, Baseline/off with the same conversation workload, Separate explicit-PID cold-start collection, Normal combined-feature workload, Matched board, build, devices, model, network, duration and utterances (+13 more)

### Community 30 - "Runtime and Setup"
Cohesion: 0.19
Nodes (5): log(), main(), read_env(), _requirements(), Updater

### Community 31 - "Runtime and Setup"
Cohesion: 0.18
Nodes (10): test_device(), camera_devices(), check_camera_permission(), _framework(), resolve_camera(), camera_candidates(), configure_audio_backend(), npu_drivers() (+2 more)

### Community 32 - "Pose and Inference"
Cohesion: 0.15
Nodes (9): associate(), decode_detections(), Detection, detector_roi(), _iou(), _rect_iou(), remove_letterbox(), warp() (+1 more)

### Community 33 - "Callable Tools"
Cohesion: 0.12
Nodes (11): execute(), execute(), enabled(), execute(), find_stations(), _miniaudio_source(), open_stream(), playable() (+3 more)

### Community 35 - "Runtime and Setup"
Cohesion: 0.11
Nodes (10): _format_transcript(), _generate_summary(), _keep_transcript_unsaved(), _mark_transcript_saved(), _memory_save_due(), _memory_saver_thread(), _memory_store(), _save_memories_on_stop() (+2 more)

### Community 36 - "Runtime and Setup"
Cohesion: 0.17
Nodes (8): _draw_angle_graph(), _draw_fall_alert(), _draw_hud(), _draw_skeleton(), _draw_trunk_line(), _ensure_model(), run(), _text()

### Community 37 - "Callable Tools"
Cohesion: 0.12
Nodes (7): attach(), _boost(), execute(), hold_for_answer(), register(), session_task(), _volume()

### Community 38 - "Runtime and Setup"
Cohesion: 0.18
Nodes (6): audio_catalog(), camera_catalog(), Handler, LocalServer, main(), open_camera()

### Community 39 - "Conversation and Audio"
Cohesion: 0.17
Nodes (6): enabled(), execute(), extend_config(), _level(), _path(), VoiceVolume

### Community 40 - "Callable Tools"
Cohesion: 0.18
Nodes (3): enabled(), FallWorker, _record()

### Community 42 - "Installation and Updates"
Cohesion: 0.18
Nodes (8): _device_setting(), home(), page_not_found(), profile_exists(), setup(), status(), switch_network(), uplink_iface()

### Community 43 - "Runtime and Setup"
Cohesion: 0.25
Nodes (6): configured_models(), PreflightError, validate_key(), main(), CredentialError, gemini_key()

### Community 44 - "Runtime and Setup"
Cohesion: 0.31
Nodes (12): cpu_ticks(), main(), monitor(), process_tree(), read(), report(), sample(), sensors() (+4 more)

### Community 45 - "Family Connections"
Cohesion: 0.21
Nodes (7): deliver_messages(), enabled(), profile_section(), refresh_profile(), register(), session_task(), start()

### Community 46 - "Callable Tools"
Cohesion: 0.23
Nodes (4): coordinates(), fetch_json(), Location, Weather

### Community 47 - "Runtime and Setup"
Cohesion: 0.26
Nodes (9): use_performance_cpus(), configuration_ready(), configure_once(), main(), pid_alive(), runtime_lock(), runtime_running(), _stop() (+1 more)

### Community 48 - "Runtime and Setup"
Cohesion: 0.17
Nodes (6): _download_memory_model(), _load_memory_embedder(), _memory_model_to_download(), download_model(), missing_packages(), model_present()

### Community 49 - "Runtime and Setup"
Cohesion: 0.27
Nodes (7): public_configuration(), read_configuration(), save_configuration(), audio_device(), camera_device(), identity(), main()

### Community 51 - "Runtime and Setup"
Cohesion: 0.23
Nodes (3): remote_pose(), remote_face(), Remote

### Community 54 - "Runtime and Setup"
Cohesion: 0.18
Nodes (6): _build_config(), _fire_initiation(), _initiation_memory_hint(), initiation_monitor(), _newest_memories(), _safe_to_initiate()

### Community 55 - "Runtime and Setup"
Cohesion: 0.36
Nodes (9): ensure_environment(), environment_python(), fingerprint(), install_shortcuts(), installation_lock(), main(), requirements_file(), run_checked() (+1 more)

### Community 56 - "SD Card Builder"
Cohesion: 0.24
Nodes (6): anydesk_id(), command(), duration(), Handler, progress(), status()

### Community 57 - "Callable Tools"
Cohesion: 0.24
Nodes (3): execute(), Listen, record()

### Community 58 - "Conversation and Audio"
Cohesion: 0.22
Nodes (5): connection_lost(), extend_config(), on_server_message(), remember(), _tokens()

### Community 60 - "Everyday Support"
Cohesion: 0.31
Nodes (7): available_to_speak(), enabled(), execute(), notify_due(), _playback_active(), register(), session_task()

### Community 61 - "Installation and Updates"
Cohesion: 0.38
Nodes (9): cfg(), pactl_user(), run_gemini_with_retry(), set_audio_levels(), startup.sh script, start_containers(), start_portal_supervised(), start_robot() (+1 more)

### Community 65 - "Runtime and Setup"
Cohesion: 0.33
Nodes (6): collect_suites(), local_settings_ignored(), __init__(), main(), render_report(), run_sections()

### Community 68 - "Pose and Inference"
Cohesion: 0.25
Nodes (4): decode_landmarks(), project(), refine_from_heatmap(), _sigmoid()

### Community 69 - "Runtime and Setup"
Cohesion: 0.25
Nodes (4): arm_linux(), performance_cpus(), main(), runtime_script()

### Community 70 - "Runtime and Setup"
Cohesion: 0.22
Nodes (3): _boot_status(), _notify_webapp(), _TurnTranscript

### Community 71 - "Runtime and Setup"
Cohesion: 0.36
Nodes (7): feature_html(), flush(), flush_paragraph(), inline(), main(), portable_diagnostic(), source_group()

### Community 72 - "Installation and Updates"
Cohesion: 0.50
Nodes (7): cfg(), compose(), healthy(), image_id(), log(), ember-update.sh script, wait_healthy()

### Community 75 - "Runtime and Setup"
Cohesion: 0.38
Nodes (3): main(), open_camera(), say()

### Community 77 - "Pose and Inference"
Cohesion: 0.29
Nodes (3): alignment_rect(), normalize_radians(), Rect

### Community 78 - "Installation and Updates"
Cohesion: 0.53
Nodes (4): closure(), imported_top_level_modules(), main(), root_distributions()

### Community 79 - "Installation and Updates"
Cohesion: 0.60
Nodes (5): cfg(), die(), run(), provision.sh script, step()

### Community 82 - "Runtime and Setup"
Cohesion: 0.53
Nodes (4): desktop_quote(), install(), main(), systemd_quote()

### Community 83 - "Runtime and Setup"
Cohesion: 0.53
Nodes (4): display_connected(), main(), open_monitor(), serve()

### Community 84 - "SD Card Builder"
Cohesion: 0.60
Nodes (4): load(), main(), save(), validate()

### Community 86 - "Installation and Updates"
Cohesion: 0.60
Nodes (3): die(), make_usb.sh script, step()

### Community 88 - "Runtime and Setup"
Cohesion: 0.40
Nodes (3): _audio_writer_thread(), _open_new_segment(), _prune_day_utterance()

### Community 89 - "Runtime and Setup"
Cohesion: 0.67
Nodes (4): Development and review protocol, Offline, live and lead-review evidence, Software-lead approval before merge/deployment, Offline core and toolkit review gate

### Community 90 - "Installation and Updates"
Cohesion: 0.83
Nodes (3): answer(), erase-confirm.sh script, stop_here()

### Community 92 - "Runtime and Setup"
Cohesion: 0.50
Nodes (4): macOS simulation and hardware verification, Simulated macOS device contract tests, Intel macOS dependency profile, Windows pinned dependency profile

## Knowledge Gaps
- **60 isolated node(s):** `Development and review protocol`, `Desktop setup and device selection`, `Intel macOS dependency profile`, `Shared Orange Pi ARM64 dependency profile`, `Desktop dependency profile` (+55 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 656 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **57 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `ChildPose` connect `Pose and Inference` to `Pose and Inference`, `Runtime and Setup`?**
  _High betweenness centrality (0.125) - this node is a cross-community bridge._
- **What connects `Development and review protocol`, `Desktop setup and device selection`, `Intel macOS dependency profile` to the rest of the system?**
  _60 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Callable Tools` be split into smaller, more focused modules?**
  _Cohesion score 0.052184769038701624 - nodes in this community are weakly interconnected._
- **Why does `PosePipeline` connect `Pose and Inference` to `Pose and Inference`, `Pose and Inference`, `Pose and Inference`, `Pose and Inference`, `Pose and Inference`?**
  _High betweenness centrality (0.067) - this node is a cross-community bridge._
- **Should `Physical Eyes` be split into smaller, more focused modules?**
  _Cohesion score 0.050203527815468114 - nodes in this community are weakly interconnected._
- **Why does `VoiceAnalyzer` connect `Runtime and Setup` to `Runtime and Setup`, `Runtime and Setup`?**
  _High betweenness centrality (0.050) - this node is a cross-community bridge._
- **Should `Everyday Support` be split into smaller, more focused modules?**
  _Cohesion score 0.07080200501253132 - nodes in this community are weakly interconnected._
## Scope and integrity

The graph is documentation, not a hardware certification. Local paths are repository-relative. 897 unresolved/external references (including standard-library imports) are omitted from this project-only view and retained in `UNRESOLVED_REFERENCES.json`; these are not claimed to be missing runtime dependencies. Parallel relationships can collapse in the native undirected view; raw semantic evidence remains in `docs/features/semantic_graph.json`.

Project-view health:

```json
{
  "node_count": 1877,
  "unverified_node_count": 0,
  "raw_edge_count": 3275,
  "non_object_edges": 0,
  "missing_endpoint_edges": 0,
  "dangling_endpoint_edges": 0,
  "external_reference_edges": 0,
  "self_loop_edges": 4,
  "valid_candidate_edges": 3275,
  "exact_duplicate_edges": 0,
  "directed_unique_endpoint_pairs": 3207,
  "directed_same_endpoint_collapsed_edges": 68,
  "undirected_unique_endpoint_pairs": 3201,
  "undirected_same_endpoint_collapsed_edges": 74,
  "same_endpoint_group_count": 67,
  "relation_variant_groups": 66,
  "source_file_variant_groups": 0,
  "source_location_variant_groups": 2,
  "context_variant_groups": 0,
  "post_build_graph_type": "Graph",
  "post_build_node_count": 1876,
  "post_build_edge_count": 3200,
  "post_build_error": "",
  "producer_suppression": {
    "path": "external-package/extract.py",
    "total_sites": 12,
    "sites": [
      {
        "line": 1789,
        "name": "seen_ids",
        "tuple_arity": 0,
        "sample": "seen_ids = {n[\"id\"] for n in nodes}"
      },
      {
        "line": 2353,
        "name": "seen_ids",
        "tuple_arity": 0,
        "sample": "seen_ids = {n[\"id\"] for n in nodes}"
      },
      {
        "line": 2355,
        "name": "seen_doc_refs",
        "tuple_arity": 0,
        "sample": "seen_doc_refs: set[str] = set()"
      },
      {
        "line": 3022,
        "name": "seen_ids",
        "tuple_arity": 0,
        "sample": "seen_ids: set[str] = {n[\"id\"] for n in nodes}"
      },
      {
        "line": 3169,
        "name": "seen_ids",
        "tuple_arity": 0,
        "sample": "seen_ids = {n.get(\"id\") for n in nodes}"
      },
      {
        "line": 4179,
        "name": "seen_keys",
        "tuple_arity": 0,
        "sample": "seen_keys: set[tuple] = set()"
      },
      {
        "line": 4365,
        "name": "seen_keys",
        "tuple_arity": 0,
        "sample": "seen_keys: set[tuple] = set()"
      },
      {
        "line": 7608,
        "name": "seen_ids",
        "tuple_arity": 0,
        "sample": "seen_ids: set[str] = set()"
      },
      {
        "line": 7716,
        "name": "seen_ids",
        "tuple_arity": 0,
        "sample": "seen_ids: set[str] = set()"
      },
      {
        "line": 7795,
        "name": "seen_ids",
        "tuple_arity": 0,
        "sample": "seen_ids: set[str] = set()"
      },
      {
        "line": 8310,
        "name": "seen_ids",
        "tuple_arity": 0,
        "sample": "seen_ids: set[str] = set()"
      },
      {
        "line": 8311,
        "name": "seen_edges",
        "tuple_arity": 4,
        "sample": "seen_edges: set[tuple[str, str, str, str | None]] = set()"
      }
    ],
    "error": ""
  },
  "examples": [
    {
      "source": "baymax_main_features_pose_validate_main",
      "target": "baymax_main_features_pose_validate_main_port",
      "edge_count": 3,
      "relations": [
        "contains",
        "indirect_call"
      ],
      "source_files": [
        "Baymax-main/features/pose/validate.py"
      ],
      "source_locations": [
        "L216",
        "L220",
        "L225"
      ],
      "contexts": [
        "",
        "collection"
      ]
    },
    {
      "source": "baymax_main_baymax_agent",
      "target": "baymax_main_baymax_cloud",
      "edge_count": 2,
      "relations": [
        "imports",
        "imports_from"
      ],
      "source_files": [
        "Baymax-main/baymax_agent.py"
      ],
      "source_locations": [
        "L26",
        "L27"
      ],
      "contexts": [
        "import"
      ]
    },
    {
      "source": "baymax_main_baymax_app_login_required",
      "target": "baymax_main_baymax_app_login_required_decorated",
      "edge_count": 2,
      "relations": [
        "contains",
        "indirect_call"
      ],
      "source_files": [
        "Baymax-main/baymax_app.py"
      ],
      "source_locations": [
        "L142",
        "L146"
      ],
      "contexts": [
        "",
        "return"
      ]
    },
    {
      "source": "baymax_main_baymax_app_send_verification_email",
      "target": "baymax_main_baymax_app_send_verification_email_send",
      "edge_count": 2,
      "relations": [
        "contains",
        "indirect_call"
      ],
      "source_files": [
        "Baymax-main/baymax_app.py"
      ],
      "source_locations": [
        "L185",
        "L204"
      ],
      "contexts": [
        "",
        "argument"
      ]
    },
    {
      "source": "baymax_main_baymax_app_camera_stream",
      "target": "baymax_main_baymax_app_camera_stream_relay",
      "edge_count": 2,
      "relations": [
        "calls",
        "contains"
      ],
      "source_files": [
        "Baymax-main/baymax_app.py"
      ],
      "source_locations": [
        "L440",
        "L450"
      ],
      "contexts": [
        "",
        "call"
      ]
    }
  ]
}
```

Raw extraction health before scope filtering:

```json
{
  "node_count": 1877,
  "unverified_node_count": 0,
  "raw_edge_count": 4172,
  "non_object_edges": 0,
  "missing_endpoint_edges": 0,
  "dangling_endpoint_edges": 897,
  "external_reference_edges": 0,
  "self_loop_edges": 4,
  "valid_candidate_edges": 3275,
  "exact_duplicate_edges": 0,
  "directed_unique_endpoint_pairs": 3207,
  "directed_same_endpoint_collapsed_edges": 68,
  "undirected_unique_endpoint_pairs": 3201,
  "undirected_same_endpoint_collapsed_edges": 74,
  "same_endpoint_group_count": 67,
  "relation_variant_groups": 66,
  "source_file_variant_groups": 0,
  "source_location_variant_groups": 2,
  "context_variant_groups": 0,
  "post_build_graph_type": "Graph",
  "post_build_node_count": 1959,
  "post_build_edge_count": 4096,
  "post_build_error": "",
  "producer_suppression": {
    "path": "external-package/extract.py",
    "total_sites": 12,
    "sites": [
      {
        "line": 1789,
        "name": "seen_ids",
        "tuple_arity": 0,
        "sample": "seen_ids = {n[\"id\"] for n in nodes}"
      },
      {
        "line": 2353,
        "name": "seen_ids",
        "tuple_arity": 0,
        "sample": "seen_ids = {n[\"id\"] for n in nodes}"
      },
      {
        "line": 2355,
        "name": "seen_doc_refs",
        "tuple_arity": 0,
        "sample": "seen_doc_refs: set[str] = set()"
      },
      {
        "line": 3022,
        "name": "seen_ids",
        "tuple_arity": 0,
        "sample": "seen_ids: set[str] = {n[\"id\"] for n in nodes}"
      },
      {
        "line": 3169,
        "name": "seen_ids",
        "tuple_arity": 0,
        "sample": "seen_ids = {n.get(\"id\") for n in nodes}"
      },
      {
        "line": 4179,
        "name": "seen_keys",
        "tuple_arity": 0,
        "sample": "seen_keys: set[tuple] = set()"
      },
      {
        "line": 4365,
        "name": "seen_keys",
        "tuple_arity": 0,
        "sample": "seen_keys: set[tuple] = set()"
      },
      {
        "line": 7608,
        "name": "seen_ids",
        "tuple_arity": 0,
        "sample": "seen_ids: set[str] = set()"
      },
      {
        "line": 7716,
        "name": "seen_ids",
        "tuple_arity": 0,
        "sample": "seen_ids: set[str] = set()"
      },
      {
        "line": 7795,
        "name": "seen_ids",
        "tuple_arity": 0,
        "sample": "seen_ids: set[str] = set()"
      },
      {
        "line": 8310,
        "name": "seen_ids",
        "tuple_arity": 0,
        "sample": "seen_ids: set[str] = set()"
      },
      {
        "line": 8311,
        "name": "seen_edges",
        "tuple_arity": 4,
        "sample": "seen_edges: set[tuple[str, str, str, str | None]] = set()"
      }
    ],
    "error": ""
  },
  "examples": [
    {
      "source": "baymax_main_features_pose_validate_main",
      "target": "baymax_main_features_pose_validate_main_port",
      "edge_count": 3,
      "relations": [
        "contains",
        "indirect_call"
      ],
      "source_files": [
        "Baymax-main/features/pose/validate.py"
      ],
      "source_locations": [
        "L216",
        "L220",
        "L225"
      ],
      "contexts": [
        "",
        "collection"
      ]
    },
    {
      "source": "baymax_main_baymax_agent",
      "target": "baymax_main_baymax_cloud",
      "edge_count": 2,
      "relations": [
        "imports",
        "imports_from"
      ],
      "source_files": [
        "Baymax-main/baymax_agent.py"
      ],
      "source_locations": [
        "L26",
        "L27"
      ],
      "contexts": [
        "import"
      ]
    },
    {
      "source": "baymax_main_baymax_app_login_required",
      "target": "baymax_main_baymax_app_login_required_decorated",
      "edge_count": 2,
      "relations": [
        "contains",
        "indirect_call"
      ],
      "source_files": [
        "Baymax-main/baymax_app.py"
      ],
      "source_locations": [
        "L142",
        "L146"
      ],
      "contexts": [
        "",
        "return"
      ]
    },
    {
      "source": "baymax_main_baymax_app_send_verification_email",
      "target": "baymax_main_baymax_app_send_verification_email_send",
      "edge_count": 2,
      "relations": [
        "contains",
        "indirect_call"
      ],
      "source_files": [
        "Baymax-main/baymax_app.py"
      ],
      "source_locations": [
        "L185",
        "L204"
      ],
      "contexts": [
        "",
        "argument"
      ]
    },
    {
      "source": "baymax_main_baymax_app_camera_stream",
      "target": "baymax_main_baymax_app_camera_stream_relay",
      "edge_count": 2,
      "relations": [
        "calls",
        "contains"
      ],
      "source_files": [
        "Baymax-main/baymax_app.py"
      ],
      "source_locations": [
        "L440",
        "L450"
      ],
      "contexts": [
        "",
        "call"
      ]
    }
  ]
}
```
