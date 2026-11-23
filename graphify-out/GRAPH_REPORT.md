[Organized OPi4 V1 feature log](../docs/features/FEATURE_LOG.md) · [Interactive graph](graph.html) · [Rendered feature log](FEATURE_LOG.html)

# Graph Report - Baymax  (2026-11-11)

## Corpus Check
- 209 files · ~193,880 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 48 file(s) not represented in the graph (top: .service 11, (none) 9, .cmd 7)

## Summary
- 1959 nodes · 3339 edges · 133 communities (80 shown, 53 thin omitted)
- Extraction: 97% EXTRACTED · 3% INFERRED · 0% AMBIGUOUS · INFERRED: 108 edges (avg confidence: 0.86)
- Token usage: structural extraction uses no LLM; semantic usage was not reported by the host.

## Community Hubs (Navigation)
- Callable Tools
- Pose and Inference
- Runtime and Setup
- Everyday Support
- Runtime and Setup
- Runtime and Setup
- Conversation and Audio
- Feature Documentation
- Callable Tools
- Pose and Inference
- Runtime and Setup
- SD Card Builder
- Conversation and Audio
- Pose and Inference
- Runtime and Setup
- Installation and Updates
- Conversation and Audio
- Pose and Inference
- Callable Tools
- Runtime and Setup
- Runtime and Setup
- Pose and Inference
- Runtime and Setup
- Callable Tools
- Feature Documentation
- Runtime and Setup
- Callable Tools
- Callable Tools
- Runtime and Setup
- Runtime and Setup
- Runtime and Setup
- Runtime and Setup
- Pose and Inference
- Conversation and Audio
- Runtime and Setup
- Runtime and Setup
- Conversation and Audio
- Callable Tools
- Runtime and Setup
- Conversation and Audio
- Callable Tools
- Callable Tools
- Physical Eyes
- Callable Tools
- Installation and Updates
- Physical Eyes
- Pose and Inference
- Everyday Support
- Family Connections
- Family Connections
- Runtime and Setup
- Runtime and Setup
- Physical Eyes
- Callable Tools
- Runtime and Setup
- Runtime and Setup
- Runtime and Setup
- Physical Eyes
- Family Connections
- Callable Tools
- Runtime and Setup
- Runtime and Setup
- SD Card Builder
- Pose and Inference
- Callable Tools
- Installation and Updates
- Runtime and Setup
- Conversation and Audio
- Physical Eyes
- Callable Tools
- Runtime and Setup
- Runtime and Setup
- Runtime and Setup
- Installation and Updates
- Conversation and Audio
- Physical Eyes
- Runtime and Setup
- Everyday Support
- Callable Tools
- Callable Tools
- Installation and Updates
- Installation and Updates
- Runtime and Setup
- Runtime and Setup
- Callable Tools
- Callable Tools
- Runtime and Setup
- Runtime and Setup
- SD Card Builder
- Installation and Updates
- Runtime and Setup
- Runtime and Setup
- Installation and Updates
- Runtime and Setup
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
1. `OPi4 V1 organized feature log` - 24 edges
2. `run()` - 22 edges
3. `VoiceAnalyzer` - 22 edges
4. `Store` - 20 edges
5. `PublicSources` - 20 edges
6. `check()` - 19 edges
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
- `Key and model metadata preflight` --references--> `Existing Gemini Live session`  [EXTRACTED]
  devops/clickables/README.md → Baymax-main/features/conversational_ability/README.md
- `Identical tested language instruction integrated in review-branch source` --references--> `Shared persona [LANGUAGE] adaptation guidance`  [EXTRACTED]
  README.md → Baymax-main/features/persona.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Shared pose and fall-check-in flow** — baymax_main_features_toolkits_fall_detection_fall_toolkit, baymax_main_features_toolkits_fall_detection_shared_pose, baymax_main_features_pose_readme_vision_child, baymax_main_features_pose_readme_pose, baymax_main_features_toolkits_fall_detection_fall_checkin [EXTRACTED 1.00]
- **Family messages and everyday reminder sync** — baymax_main_features_toolkits_family_readme_family, baymax_main_features_toolkits_family_readme_app_sync, baymax_main_features_toolkits_family_readme_family_store, baymax_main_features_toolkits_everyday_readme_everyday, baymax_main_features_toolkits_family_readme_confirmed_replies [EXTRACTED 1.00]

## Communities (133 total, 53 thin omitted)

### Community 0 - "Callable Tools"
Cohesion: 0.05
Nodes (29): ConversationSources, discovery_enabled(), enabled_names(), extend_config(), handle_tool_call(), run_call(), Toolkit, Headlines (+21 more)

### Community 1 - "Pose and Inference"
Cohesion: 0.05
Nodes (24): compare_raw(), alignment_rect(), associate(), decode_detections(), decode_landmarks(), Detection, detector_roi(), _iou() (+16 more)

### Community 2 - "Runtime and Setup"
Cohesion: 0.06
Nodes (40): classify(), compile_report(), digest(), evidence(), load_inputs(), main(), review_template(), run_passes() (+32 more)

### Community 3 - "Everyday Support"
Cohesion: 0.07
Nodes (19): _at(), _candidates(), _clock(), _days_in_month(), describe(), device_timezone(), _ints(), local_start() (+11 more)

### Community 4 - "Runtime and Setup"
Cohesion: 0.07
Nodes (38): _alert_recipients(), auth_login(), auth_logout(), auth_me(), auth_resend(), auth_signup(), auth_verify(), camera_stream() (+30 more)

### Community 5 - "Runtime and Setup"
Cohesion: 0.07
Nodes (20): _build_transcript(), _collect_segments(), main(), _merge_segments(), _poll_jobs(), _report_failure(), _run_analysis(), _enqueue() (+12 more)

### Community 6 - "Conversation and Audio"
Cohesion: 0.06
Nodes (15): apply(), audio_ready(), enabled(), Engagement, heard(), _listener(), register(), cpu_factory() (+7 more)

### Community 7 - "Feature Documentation"
Cohesion: 0.06
Nodes (51): Independent Ember and Monitor user services, MVP startup and update guide, Opt-in release updater with rollback, On-board device settings, PortAudio / PipeWire / WirePlumber audio packages, Compiler, Python, Git and network system packages, XFCE / LightDM / Xorg desktop packages, Image system package list (+43 more)

### Community 8 - "Callable Tools"
Cohesion: 0.06
Nodes (14): remote_pose(), ensure_model(), EyeContact, FaceLandmarks, looking(), presence_enabled(), Proactive, remote_face() (+6 more)

### Community 9 - "Pose and Inference"
Cohesion: 0.08
Nodes (21): ChildPose, _close_quietly(), create_pose_backend(), driver_hint(), _driver_hint(), _flat(), installed_rknnlite_version(), npu_unavailable_reason() (+13 more)

### Community 10 - "Runtime and Setup"
Cohesion: 0.08
Nodes (23): latest_frames_only(), _abandon_reply(), _append_playback(), _build_hazard_table(), _draw_angle_graph(), _draw_annotated(), _draw_fall_alert(), _draw_hud() (+15 more)

### Community 11 - "SD Card Builder"
Cohesion: 0.11
Nodes (23): asset(), digest(), generate_credentials(), attach(), chroot_down(), chroot_up(), detach(), build_linux.sh script (+15 more)

### Community 12 - "Conversation and Audio"
Cohesion: 0.09
Nodes (9): ResidualEcho, create(), enabled(), extend_config(), fan_filter_hz(), FanFilter, InputEnhancer, mute_during_reply() (+1 more)

### Community 13 - "Pose and Inference"
Cohesion: 0.12
Nodes (24): build_dir(), build_rknn(), check(), check_failures(), check_limits(), check_sequences(), compare_onnx_tflite(), compare_pipelines() (+16 more)

### Community 14 - "Runtime and Setup"
Cohesion: 0.10
Nodes (22): transcript_overlap(), _abort_connection(), listen_audio(), receive_audio(), play_reply_audio(), show_reply_text(), run(), send_audio_realtime() (+14 more)

### Community 15 - "Installation and Updates"
Cohesion: 0.13
Nodes (23): block_ssid(), client_mode(), hostapd_config(), hotspot_name(), join_saved(), log(), main(), network_block() (+15 more)

### Community 16 - "Conversation and Audio"
Cohesion: 0.09
Nodes (5): AddressGate, follow_up_seconds(), is_speech(), names_ember(), _WatchedSession

### Community 17 - "Pose and Inference"
Cohesion: 0.10
Nodes (10): import_rknnlite(), _rknn_engine(), _by_size(), _finite(), heatmap_hwc(), OnnxEngine, RknnEngine, rknnlite_class() (+2 more)

### Community 18 - "Callable Tools"
Cohesion: 0.11
Nodes (14): _clean_query(), _covers_query(), download(), fetch_book(), _ia_search(), _keep_only(), _lucene_terms(), _normalise_title() (+6 more)

### Community 19 - "Runtime and Setup"
Cohesion: 0.11
Nodes (7): FallDetector, main(), open_camera(), say(), CpuPose, _detector_factory(), ensure_model()

### Community 20 - "Runtime and Setup"
Cohesion: 0.14
Nodes (24): AI core and webapp container composition, Experimental Docker deployment, Experimental core/webapp containers, Host boot and optional Wi-Fi hotspot tooling, Synthetic Gemini-in-the-loop evaluation, MediaPipe fall detection setup, Integrated v10 MVP installation, Orange Pi board-specific Debian setup (+16 more)

### Community 21 - "Pose and Inference"
Cohesion: 0.15
Nodes (18): compare(), fall_detector(), _jsonable(), main(), pct(), _print_clip(), _print_summary(), _rate() (+10 more)

### Community 22 - "Runtime and Setup"
Cohesion: 0.12
Nodes (24): Editing and validation protocol, Draft PR and software-lead review before merge/deployment, Preserve user edits, import paths and data flows, Board-specific live and fresh-card validation, Orange Pi scope: Pallas-Robotics/Baymax focused branch, Keep credentials, recordings, models and private images out of Git and graphs, Graphify AST extraction plus reviewed semantic document snapshot, Graphify feature map guide (+16 more)

### Community 23 - "Callable Tools"
Cohesion: 0.13
Nodes (8): CheckIns, enabled(), _last_conversation(), _last_heard(), _path(), _setting(), window(), windows()

### Community 24 - "Feature Documentation"
Cohesion: 0.12
Nodes (23): Sampler recognizes A733 viplite/core_loading and RK3588 rknpu load, Full candidate matrix: text-mode evidence, no validated audio simulations, Punctuation-only Spanish and empty/incomplete English input transcripts, Pinned model/config-specific simulation evidence, System CPU baseline 22.69/24.97%, tests 25.8/27.97% mean/peak, Orange Pi language simulation results — 2026-11-06, Text-turn-to-first-audio: median 1.48 s, p95 2.62 s, max 4.58 s, Orange Pi 4 Pro: ARM64, 8 CPU cores, 3903.7 MiB usable RAM (+15 more)

### Community 25 - "Runtime and Setup"
Cohesion: 0.15
Nodes (22): Competent-speaker review of every turn, Full candidate matrix and explicit valid_audio_languages output, Switch from native language to English, Human review bound to exact model/config/input/transcript/audio hashes, Multilingual evaluation and validated-language guide, Mixed Hindi-English scripted scenario, Follow-up in the same language, Scripted native-language question (+14 more)

### Community 26 - "Callable Tools"
Cohesion: 0.14
Nodes (13): execute(), execute(), enabled(), execute(), extend_config(), handle_tool_call(), _keep_warm(), prewarm() (+5 more)

### Community 27 - "Callable Tools"
Cohesion: 0.14
Nodes (4): coordinates(), fetch_json(), Location, Weather

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
Cohesion: 0.13
Nodes (7): default_mediapipe_options(), MediaPipeCpuBackend, frames_from(), main(), run(), summary(), Timed

### Community 34 - "Runtime and Setup"
Cohesion: 0.11
Nodes (10): _format_transcript(), _generate_summary(), _keep_transcript_unsaved(), _mark_transcript_saved(), _memory_save_due(), _memory_saver_thread(), _memory_store(), _save_memories_on_stop() (+2 more)

### Community 35 - "Runtime and Setup"
Cohesion: 0.17
Nodes (8): _draw_angle_graph(), _draw_fall_alert(), _draw_hud(), _draw_skeleton(), _draw_trunk_line(), _ensure_model(), run(), _text()

### Community 36 - "Conversation and Audio"
Cohesion: 0.14
Nodes (9): connection_lost(), connection_not_started(), extend_config(), _leaves(), not_tried(), on_server_message(), remember(), retry_plan() (+1 more)

### Community 37 - "Callable Tools"
Cohesion: 0.13
Nodes (7): find_stations(), _miniaudio_source(), open_stream(), playable(), public_url(), stream_station(), UnsupportedStream

### Community 38 - "Runtime and Setup"
Cohesion: 0.18
Nodes (6): audio_catalog(), camera_catalog(), Handler, LocalServer, main(), open_camera()

### Community 39 - "Conversation and Audio"
Cohesion: 0.17
Nodes (6): enabled(), execute(), extend_config(), _level(), _path(), VoiceVolume

### Community 40 - "Callable Tools"
Cohesion: 0.13
Nodes (6): attach(), _boost(), register(), session_task(), tell_gemini(), _volume()

### Community 41 - "Callable Tools"
Cohesion: 0.18
Nodes (3): enabled(), FallWorker, _record()

### Community 42 - "Physical Eyes"
Cohesion: 0.16
Nodes (7): clear_failures(), expected_sensors(), eyes_session(), sensors(), failure_face(), lost_sensor(), _mic_reports()

### Community 44 - "Installation and Updates"
Cohesion: 0.18
Nodes (8): _device_setting(), home(), page_not_found(), profile_exists(), setup(), status(), switch_network(), uplink_iface()

### Community 45 - "Physical Eyes"
Cohesion: 0.28
Nodes (5): LedControl, crashed(), main(), settings(), show()

### Community 46 - "Pose and Inference"
Cohesion: 0.13
Nodes (4): _Backend, FallbackBackend, PipelineBackend, port()

### Community 47 - "Everyday Support"
Cohesion: 0.20
Nodes (9): hold_for_answer(), available_to_speak(), enabled(), execute(), notify_due(), _playback_active(), register(), session_task() (+1 more)

### Community 48 - "Family Connections"
Cohesion: 0.17
Nodes (7): deliver_messages(), enabled(), profile_section(), refresh_profile(), register(), session_task(), start()

### Community 50 - "Runtime and Setup"
Cohesion: 0.25
Nodes (6): configured_models(), PreflightError, validate_key(), main(), CredentialError, gemini_key()

### Community 51 - "Runtime and Setup"
Cohesion: 0.31
Nodes (12): cpu_ticks(), main(), monitor(), process_tree(), read(), report(), sample(), sensors() (+4 more)

### Community 52 - "Physical Eyes"
Cohesion: 0.23
Nodes (6): attach(), AutoEyes, cue(), medical_alert(), reply_cue(), requested()

### Community 54 - "Runtime and Setup"
Cohesion: 0.26
Nodes (9): use_performance_cpus(), configuration_ready(), configure_once(), main(), pid_alive(), runtime_lock(), runtime_running(), _stop() (+1 more)

### Community 55 - "Runtime and Setup"
Cohesion: 0.17
Nodes (6): _download_memory_model(), _load_memory_embedder(), _memory_model_to_download(), download_model(), missing_packages(), model_present()

### Community 56 - "Runtime and Setup"
Cohesion: 0.27
Nodes (7): public_configuration(), read_configuration(), save_configuration(), audio_device(), camera_device(), identity(), main()

### Community 60 - "Runtime and Setup"
Cohesion: 0.18
Nodes (6): _build_config(), _fire_initiation(), _initiation_memory_hint(), initiation_monitor(), _newest_memories(), _safe_to_initiate()

### Community 61 - "Runtime and Setup"
Cohesion: 0.36
Nodes (9): ensure_environment(), environment_python(), fingerprint(), install_shortcuts(), installation_lock(), main(), requirements_file(), run_checked() (+1 more)

### Community 62 - "SD Card Builder"
Cohesion: 0.24
Nodes (6): anydesk_id(), command(), duration(), Handler, progress(), status()

### Community 63 - "Pose and Inference"
Cohesion: 0.27
Nodes (11): Fall package requirements, MediaPipe CPU pose fallback, Pose backend selection, Conditional RK3588 NPU pose, Pose inference child process, Pose port validation evidence, Optional fall-detection toolkit, Target-board fall accuracy checks (+3 more)

### Community 65 - "Installation and Updates"
Cohesion: 0.38
Nodes (9): cfg(), pactl_user(), run_gemini_with_retry(), set_audio_levels(), startup.sh script, start_containers(), start_portal_supervised(), start_robot() (+1 more)

### Community 66 - "Runtime and Setup"
Cohesion: 0.33
Nodes (6): collect_suites(), local_settings_ignored(), __init__(), main(), render_report(), run_sections()

### Community 69 - "Callable Tools"
Cohesion: 0.31
Nodes (9): Allowlisted bounded HTTPS transport, On-demand conversation discovery, TheMealDB recipe lookup, PoetryDB poem lookup, Radio Browser discovery provider, TVmaze show lookup, Requested local brain games, Open Trivia DB trivia batches (+1 more)

### Community 70 - "Runtime and Setup"
Cohesion: 0.25
Nodes (4): arm_linux(), performance_cpus(), main(), runtime_script()

### Community 71 - "Runtime and Setup"
Cohesion: 0.22
Nodes (3): _boot_status(), _notify_webapp(), _TurnTranscript

### Community 72 - "Runtime and Setup"
Cohesion: 0.36
Nodes (7): feature_html(), flush(), flush_paragraph(), inline(), main(), portable_diagnostic(), source_group()

### Community 73 - "Installation and Updates"
Cohesion: 0.50
Nodes (7): cfg(), compose(), healthy(), image_id(), log(), ember-update.sh script, wait_healthy()

### Community 74 - "Conversation and Audio"
Cohesion: 0.36
Nodes (8): Optional strict address gate, Whole-frame PCM processing, Conversation and turn-taking, Existing Gemini Live session, Context compression and session resumption, WebRTC echo cancellation and cleanup, Joe language cache profile and network retry OPI4 candidate, Voice control follow up software and synthetic provider evidence

### Community 75 - "Physical Eyes"
Cohesion: 0.29
Nodes (4): enabled(), execute(), extend_config(), silent()

### Community 76 - "Runtime and Setup"
Cohesion: 0.29
Nodes (8): LED eye lifecycle and animation, Local transcript-driven expressions, MAX7219 matrices and Firmata, Reserved Memory Palace namespace, Reserved person-recognition boundary, Ember core feature systems, Ember personality prompt, Uncertain possible-fall conversation check-in

### Community 77 - "Everyday Support"
Cohesion: 0.25
Nodes (8): Everyday clocks, reminders and lists, Local everyday SQLite persistence, Wall-clock repeating reminders, Gemini-connected reminder delivery, Device inbox and sync API, Confirmed outgoing family replies, Optional Ember app link, Local family message SQLite store

### Community 80 - "Installation and Updates"
Cohesion: 0.53
Nodes (4): closure(), imported_top_level_modules(), main(), root_distributions()

### Community 81 - "Installation and Updates"
Cohesion: 0.60
Nodes (5): cfg(), die(), run(), provision.sh script, step()

### Community 82 - "Runtime and Setup"
Cohesion: 0.40
Nodes (6): Local speech and latency estimates, Local device and key setup UI, Live conversation debugger, One updating bubble per toolkit invocation, Setup, Monitor, Start and Stop launchers, Key and model metadata preflight

### Community 84 - "Callable Tools"
Cohesion: 0.33
Nodes (6): Opt-in public-information experiment, Gemini built-in public-topic search, IPWho approximate location, Location and weather tools, Open-Meteo geocoding and weather, Callable Ember toolkit

### Community 86 - "Runtime and Setup"
Cohesion: 0.53
Nodes (4): desktop_quote(), install(), main(), systemd_quote()

### Community 87 - "Runtime and Setup"
Cohesion: 0.53
Nodes (4): display_connected(), main(), open_monitor(), serve()

### Community 88 - "SD Card Builder"
Cohesion: 0.60
Nodes (4): load(), main(), save(), validate()

### Community 90 - "Installation and Updates"
Cohesion: 0.60
Nodes (3): die(), make_usb.sh script, step()

### Community 91 - "Runtime and Setup"
Cohesion: 0.40
Nodes (3): _audio_writer_thread(), _open_new_segment(), _prune_day_utterance()

### Community 92 - "Runtime and Setup"
Cohesion: 0.67
Nodes (4): Development and review protocol, Offline, live and lead-review evidence, Software-lead approval before merge/deployment, Offline core and toolkit review gate

### Community 93 - "Installation and Updates"
Cohesion: 0.83
Nodes (3): answer(), erase-confirm.sh script, stop_here()

### Community 94 - "Runtime and Setup"
Cohesion: 0.50
Nodes (4): macOS simulation and hardware verification, Simulated macOS device contract tests, Intel macOS dependency profile, Windows pinned dependency profile

### Community 102 - "Runtime and Setup"
Cohesion: 0.67
Nodes (3): Shared persona [LANGUAGE] adaptation guidance, Source integration is separate from robot deployment, Identical tested language instruction integrated in review-branch source

## Knowledge Gaps
- **80 isolated node(s):** `Development and review protocol`, `Desktop setup and device selection`, `Intel macOS dependency profile`, `Shared Orange Pi ARM64 dependency profile`, `Desktop dependency profile` (+75 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 687 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **53 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `ChildPose` connect `Pose and Inference` to `Runtime and Setup`?**
  _High betweenness centrality (0.110) - this node is a cross-community bridge._
- **What connects `Development and review protocol`, `Desktop setup and device selection`, `Intel macOS dependency profile` to the rest of the system?**
  _80 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Callable Tools` be split into smaller, more focused modules?**
  _Cohesion score 0.052184769038701624 - nodes in this community are weakly interconnected._
- **Why does `PosePipeline` connect `Pose and Inference` to `Pose and Inference`, `Pose and Inference`, `Pose and Inference`?**
  _High betweenness centrality (0.058) - this node is a cross-community bridge._
- **Should `Pose and Inference` be split into smaller, more focused modules?**
  _Cohesion score 0.05 - nodes in this community are weakly interconnected._
- **Why does `VoiceAnalyzer` connect `Runtime and Setup` to `Runtime and Setup`, `Runtime and Setup`?**
  _High betweenness centrality (0.030) - this node is a cross-community bridge._
- **Should `Runtime and Setup` be split into smaller, more focused modules?**
  _Cohesion score 0.060109289617486336 - nodes in this community are weakly interconnected._
## Scope and integrity

The graph is documentation, not a hardware certification. Local paths are repository-relative. 916 unresolved/external references (including standard-library imports) are omitted from this project-only view and retained in `UNRESOLVED_REFERENCES.json`; these are not claimed to be missing runtime dependencies. Parallel relationships can collapse in the native undirected view; raw semantic evidence remains in `docs/features/semantic_graph.json`.

Project-view health:

```json
{
  "node_count": 1960,
  "unverified_node_count": 0,
  "raw_edge_count": 3414,
  "non_object_edges": 0,
  "missing_endpoint_edges": 0,
  "dangling_endpoint_edges": 0,
  "external_reference_edges": 0,
  "self_loop_edges": 5,
  "valid_candidate_edges": 3414,
  "exact_duplicate_edges": 0,
  "directed_unique_endpoint_pairs": 3346,
  "directed_same_endpoint_collapsed_edges": 68,
  "undirected_unique_endpoint_pairs": 3340,
  "undirected_same_endpoint_collapsed_edges": 74,
  "same_endpoint_group_count": 67,
  "relation_variant_groups": 65,
  "source_file_variant_groups": 0,
  "source_location_variant_groups": 3,
  "context_variant_groups": 1,
  "post_build_graph_type": "Graph",
  "post_build_node_count": 1959,
  "post_build_edge_count": 3339,
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
  "node_count": 1960,
  "unverified_node_count": 0,
  "raw_edge_count": 4330,
  "non_object_edges": 0,
  "missing_endpoint_edges": 0,
  "dangling_endpoint_edges": 916,
  "external_reference_edges": 0,
  "self_loop_edges": 5,
  "valid_candidate_edges": 3414,
  "exact_duplicate_edges": 0,
  "directed_unique_endpoint_pairs": 3346,
  "directed_same_endpoint_collapsed_edges": 68,
  "undirected_unique_endpoint_pairs": 3340,
  "undirected_same_endpoint_collapsed_edges": 74,
  "same_endpoint_group_count": 67,
  "relation_variant_groups": 65,
  "source_file_variant_groups": 0,
  "source_location_variant_groups": 3,
  "context_variant_groups": 1,
  "post_build_graph_type": "Graph",
  "post_build_node_count": 2042,
  "post_build_edge_count": 4254,
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
