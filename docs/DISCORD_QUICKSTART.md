# Discord quick-start messages

Copy each block below as a separate Discord message. These instructions match this repository's `release` branch (Pallas-Robotics/Baymax, the Orange Pi fork of JoeKojima/Baymax), not an approved production deployment.

```text
**Ember: download and run**
1. Download the latest ZIP: https://github.com/Pallas-Robotics/Baymax/archive/refs/heads/release.zip
2. Extract the ENTIRE ZIP into a permanent folder, e.g. Desktop/Ember. Don't run files inside the ZIP.
3. Open devops -> clickables -> Windows, macOS or Linux.
4. Open **1 - Setup** (.cmd on Windows, .command on Mac, .sh on Linux). Wait while it installs private Python 3.12, packages/model and runs checks. Internet is required; your existing Python isn't replaced.
5. In Setup, paste your own Gemini key, select/test mic, speaker and camera, then **Save configuration**.
Optional: enable experimental fall detection in Setup, select a camera, Save and restart. No external alerts; CPU unless the optional Orange Pi NPU setup is done; real-Pi fall validation is pending.
6. Use **Desktop/clickables**: **2 - Monitor -> 3 - Start Ember -> 4 - Stop Ember**. Wait for Connected before talking. Keep the launcher/server windows open while using them.

Later sessions only need Monitor/Start/Stop. Reopen Setup to change devices/key. Don't move the extracted code after setup; rerun Setup if you move it.

Windows installation is tested. Intel Macs now automatically use an experimental compatible dependency profile through the same macOS Setup clickable. Actual Intel/Apple Silicon Mac installations, older macOS versions and Linux hardware remain unverified. Orange Pi 5 Max/4 Pro on Debian 13 use an experimental ARM profile: open Linux/0 - Debian Prerequisites before Setup on minimal installs (asks permission for system packages); a ZIP never updates itself, so for updates use a git checkout (Baymax-main/ORANGE_PI_SETUP.md). Actual Pi installation/performance is unverified. Your OS may ask permission to open scripts/use devices; contact your lead if company policy blocks it.

Ember sends mic/camera data to Gemini when started and records session audio locally. Location/weather uses external services.
```

```text
**Create your own Gemini API key**
1. Open https://aistudio.google.com/apikey and sign in with your Google account. Accept the terms if prompted.
2. Use the default key if Google creates one, or click **Create API key** and select/create your development project. Name it **Ember Development**.
3. Copy the key into Ember **1 - Setup -> Gemini API key -> Save configuration**. Restart Ember after changing it.

Keep your key private: never post it in Discord, GitHub, screenshots or AI chats. If it is exposed, revoke it and create a replacement. Ask Natey/James before enabling paid billing.

Official key instructions: https://ai.google.dev/gemini-api/docs/api-key
```

```text
**Check Ember's Gemini usage and limits**
1. Open https://aistudio.google.com/rate-limit and select the SAME project that owns the key you saved in Ember.
2. Find **Gemini 2.5 Flash Native Audio / Live API** for conversation (configured model: gemini-2.5-flash-native-audio-preview-12-2025).
   Also find **gemini-3.8-live**: it replaces 2.5 (shut down as early as 2026-11-17) and Ember moves to it when 2.5 can't be used, so the project needs Live quota for it.
3. Also check **Gemini 2.5 Flash** for summaries. Use the usage/chart controls and time range to inspect each model's activity and limits.
4. If you see quota/rate-limit errors (429), check the relevant model's limits before retrying. An Unlimited request column does not mean every limit is unlimited.

Limits are shared across keys/devices in the project. Daily request quotas reset at midnight Pacific time. Startup's key check doesn't guarantee Live-session quota.

Quota reference: https://ai.google.dev/gemini-api/docs/rate-limits

For help, share your OS, the step that failed and the error text. Remove keys and private conversation data first.
```
