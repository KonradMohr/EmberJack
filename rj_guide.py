#!/usr/bin/env python3
"""
EmberJack Complete Built-In User Guide & Reference Content Engine
-----------------------------------------------------------------
Pure-stdlib structured documentation covering every EmberJack menu, utility,
hotkey, payload category, hardware workflow, and Raspberry Pi troubleshooting
step. Used by `payloads/utilities/guide.py` (accessible from the Main Menu).

Each topic uses structured blocks so the renderer can style headings, paragraphs,
keybind pills, bullet lists, and callout boxes cleanly at any screen size:
  - ("h", "Section Title")
  - ("p", "Descriptive paragraph text...")
  - ("k", "Shortcut / Key", "What it does")
  - ("b", "Bullet item text")
  - ("tip", "Helpful tip or best practice")
  - ("warn", "Safety / hardware requirement note")
"""

from typing import List, Tuple

import rj_tool_catalog

GUIDE_TABS: Tuple[str, ...] = ("all", "start", "apps", "net", "payloads", "tips")
GUIDE_TAB_LABELS = {
    "all": "ALL",
    "start": "START",
    "apps": "APPS",
    "net": "NET",
    "payloads": "PAYLOADS",
    "tips": "TIPS",
}

Block = Tuple[str, ...]

GUIDE_TOPICS: List[dict] = [
    # =========================================================================
    # TAB: START — Getting Started & Controls
    # =========================================================================
    {
        "id": "welcome",
        "tab": "start",
        "tag": "START",
        "title": "Welcome & Navigation",
        "summary": "How EmberJack works, view modes, live search, and the HUD.",
        "blocks": [
            ("h", "What Is EmberJack?"),
            ("p", "EmberJack is a portable Linux security, network auditing, wireless, radio, and everyday utility workstation. In HDMI mode it runs full-screen on your monitor with full USB/Bluetooth keyboard support — including built-in Terminal, standalone GUI Apps, Bluetooth & Audio managers, YouTube/YT Music, and 329+ payloads without needing a heavy desktop environment."),
            ("h", "Three Main Menu View Modes"),
            ("b", "List View: Split-pane layout with menu items on the left and a live inspector card on the right."),
            ("b", "Grid View: App-card grid layout for fast visual browsing across pages."),
            ("b", "Carousel View: Focused card view with large icons and feature summaries."),
            ("k", "Space / F1", "Cycle view mode on the Main Menu (List -> Grid -> Carousel)"),
            ("k", "Up / Down / Arrows", "Move selection; PageUp / PageDown / Home / End jump quickly"),
            ("k", "Enter / Right", "Open highlighted menu, tool, or payload"),
            ("k", "Esc / Backspace / Left", "Go back one menu or exit the current screen"),
            ("h", "Instant Menu Search"),
            ("p", "On any menu screen, simply start typing letters on your keyboard to filter across all payloads immediately. Press Backspace to delete characters, Enter to launch the highlighted match, or Esc to clear search."),
            ("h", "HDMI Host Shortcuts"),
            ("k", "F10", "Toggle the bottom HDMI status HUD bar on/off"),
            ("k", "F12", "Save a PNG screenshot into the loot/screenshots folder"),
            ("k", "F2 / Ctrl+T", "Open the integrated interactive Linux Terminal from Main Menu"),
            ("tip", "Double-press F3 / Esc on the Main Menu if configured, or open Lock to protect your device with a PIN or button sequence."),
        ],
    },
    {
        "id": "keyboard_modes",
        "tab": "start",
        "tag": "KEYS",
        "title": "Keyboard & Input Modes",
        "summary": "Navigation Mode vs Text-Input Mode and universal hotkeys.",
        "blocks": [
            ("h", "Two Smart Input Modes"),
            ("p", "EmberJack automatically switches between Navigation Mode (for menus and classic payloads) and Text-Input Mode (for Terminal, Search bars, YouTube, YT Music, App Launcher, and Bluetooth Manager) so typing letters never accidentally triggers menu buttons."),
            ("h", "Global Menu & Payload Keys (1-2 Keys Per Action)"),
            ("k", "Arrow Keys", "Navigate menus, lists, tabs, and sliders (Alt+Up/Dn pages on CardputerZero)"),
            ("k", "Enter / OK", "Confirm / Open / Select"),
            ("k", "Esc / KEY3", "Back / Cancel / Exit"),
            ("k", "KEY1 / App Key", "Primary action (Search '/', Scan 'S', Play 'Space', or Ctrl+1 on Cardputer)"),
            ("k", "KEY2 / App Key", "Secondary action or Options Menu ('O', 'Tab', or Ctrl+2 on Cardputer)"),
            ("k", "PageUp / PageDown", "Fast page scrolling in long lists, logs, and guides"),
            ("h", "On-Screen & Hardware Typing"),
            ("p", "Whenever an app asks for text (WiFi password, YouTube search, custom app command), you can type directly on HDMI or CardputerZero keyboards with full Shift, CapsLock, symbols, and Backspace support — or use the auto-shown digital keyboard on 128x128 / 240x240 SPI button HATs (toggle anytime with Tab)."),
            ("tip", "Every utility exposes all essential actions through OK / KEY1 / KEY2 / KEY3 on 8-button SPI HATs, compact keyboard shortcuts on CardputerZero, and clean 1-2 key shortcuts on HDMI."),
        ],
    },

    # =========================================================================
    # TAB: APPS — Terminal, App Launcher, Mouse, Bluetooth, Media
    # =========================================================================
    {
        "id": "terminal",
        "tab": "apps",
        "tag": "SHELL",
        "title": "Integrated Terminal",
        "summary": "Full interactive VT100/ANSI Linux shell with history & scrollback.",
        "blocks": [
            ("h", "What It Is For"),
            ("p", "The built-in Terminal gives you a real persistent bash shell inside EmberJack's UI (backed by a Linux PTY and pure-Python VT100/ANSI color emulator). Your shell session stays alive between visits so background work and directory changes remain intact."),
            ("h", "How To Use It"),
            ("k", "F2 / Ctrl+T", "Launch Terminal directly from the Main Menu (or select Terminal)"),
            ("k", "Normal Typing", "Full shell input including Tab completion, pipes, arrows, and Ctrl+C"),
            ("k", "F1", "Open Quick Commands menu (ip a, nmcli, lsusb, htop, dmesg, etc.)"),
            ("k", "Shift + PgUp / PgDn", "Scroll up and down through terminal scrollback history"),
            ("k", "Ctrl+L", "Clear the visible screen"),
            ("k", "Ctrl+Q / F3", "Leave Terminal and return to EmberJack (keeps shell alive)"),
            ("tip", "You can also type 'exit' to close the shell session completely; reopening Terminal starts a fresh shell."),
        ],
    },
    {
        "id": "app_launcher",
        "tab": "apps",
        "tag": "GUI",
        "title": "GUI App Launcher",
        "summary": "Run standalone Linux GUI apps, AppImages & custom apps with keyboard mouse.",
        "blocks": [
            ("h", "What It Is For"),
            ("p", "The Apps launcher discovers installed Linux GUI programs (.desktop entries, Flatpaks, Snaps, AppImages in ~/Applications or loot/Apps/AppImages/, and custom commands) and sorts them by Favorites -> Recently Launched -> A-Z. On headless Pi it runs ONE app fullscreen on VT7 via cage (with gamescope, weston, or xinit fallback) — or launches directly on an active Wayland/X11 desktop — without ever loading a heavy desktop environment."),
            ("h", "Browsing & Launching"),
            ("k", "Up / Down / PgUp / PgDn", "Scroll installed apps (split preview shows icon, description & command)"),
            ("k", "Left / Right / Tab", "Filter categories: ALL, FAV, WEB, MEDIA, GAMES, TOOLS (moves columns in Grid)"),
            ("k", "Enter / OK", "Launch the highlighted GUI app standalone fullscreen"),
            ("k", "/  or  KEY1", "Search installed apps by name, description, or source"),
            ("k", "O  or  KEY2", "Options: Scale, Launch Backend (Labwc / Cage / Gamescope / X11), Pointer/Game Mode, Look Preset, Mouse Speed, Sensitivity, View, Custom App & Rescan"),
            ("k", "F", "Quick Star / Unstar favorite apps (also in Options for HATs)"),
            ("k", "-  /  =", "Quick Window Render Scale smaller or larger (50% .. 250%, saved automatically)"),
            ("k", "Esc / KEY3", "Clear active search, or exit App Launcher"),
            ("h", "Inside a Launched App (Quick Controls)"),
            ("k", "ScrollLock / Ctrl+Alt+M", "Toggle Typing <-> Preferred Pointer Mode (Desktop for apps, Game Mode for 3D games)"),
            ("k", "Ctrl + Alt + G", "Toggle Dual-Hand Game Mode directly (WASD passthrough + 125 Hz 3D camera)"),
            ("k", "Alt + F4  /  Ctrl + Q", "Close the app normally and return to EmberJack"),
            ("k", "Ctrl + Alt + Q", "Force-close ANY stuck/fullscreen GUI app back to EmberJack"),
            ("h", "Installing & Adding New GUI Apps"),
            ("b", "APT packages: Open Terminal and run 'sudo apt update && sudo apt install <app>' (for example firefox, chromium, vlc, gimp, wireshark, mousepad), then open O -> Rescan Installed Apps."),
            ("b", "AppImages: Drop any .AppImage file into ~/Applications/ or loot/Apps/AppImages/ (auto-marked executable on launch) and rescan."),
            ("b", "Custom Apps / Web Apps: Press O -> + Add Custom App / AppImage to save any command (e.g. 'chromium https://example.com'). Remove custom entries anytime via O -> Remove Custom App."),
            ("tip", "Press O -> Mouse & Gaming Controls Help inside Apps anytime for a quick cheat-sheet, or read the next chapter (Keyboard Mouse & Gaming) for full Desktop & Dual-Hand Game Mode controls."),
            ("warn", "Headless Raspberry Pi requires 'cage', 'labwc', 'xwayland', and 'seatd' (installed automatically by install_raspyjack.sh --hdmi). Launch logs are saved to /dev/shm/rj_app_launcher.log."),
        ],
    },
    {
        "id": "kbd_mouse",
        "tab": "apps",
        "tag": "MOUSE",
        "title": "Keyboard Mouse & Gaming",
        "summary": "Clean Desktop cursor mode, Dual-Hand 3D Game Mode & universal app exit.",
        "blocks": [
            ("h", "How EmberJack's 3-Mode Input Engine Works"),
            ("p", "Whenever you launch a GUI app or game on headless HDMI, EmberJack creates BOTH a Linux virtual mouse and a virtual passthrough keyboard (/dev/uinput). Every app always starts cleanly in normal Typing Mode so nothing grabs your keys at startup. Press ScrollLock or Ctrl+Alt+M whenever you want cursor or 3D camera control!"),
            ("h", "Core Mode & Exit Hotkeys"),
            ("k", "ScrollLock / Ctrl+Alt+M", "Toggle Typing <-> Preferred Mode (Desktop for apps, Game Mode for 3D games)"),
            ("k", "Ctrl + Alt + G", "Toggle Dual-Hand Game Mode ON / OFF directly inside any app"),
            ("k", "Esc (Desktop Mode only)", "Return from Desktop Mouse Mode back to Typing Mode (Esc passes through in Game Mode!)"),
            ("k", "Ctrl + Alt + Q", "Force-close ANY GUI app or game and return straight to EmberJack"),
            ("h", "Desktop Mouse Mode (Clean 1-2 Keys Per Action)"),
            ("k", "Arrows", "Move cursor smoothly (hold LShift = slow precision, LCtrl = fast)"),
            ("k", "Space / Enter", "Left Click (hold key down to click-and-drag)"),
            ("k", "Backspace", "Right Click (context menus)"),
            ("k", "Tab", "Middle Click (open link in new tab / paste)"),
            ("k", "PgUp / PgDn  (or [ / ])", "Scroll Up / Down"),
            ("k", "-  /  =", "Decrease / Increase Desktop Mouse Speed live (1..10, auto-saved)"),
            ("h", "Dual-Hand Game Mode (Simultaneous Walk + 125 Hz 3D Look!)"),
            ("p", "In Game Mode, your left hand (WASD, Space, LShift, LCtrl, E, Q, R, F, 1-9, Tab, Esc) passes straight through to the game at <1 ms latency while ONLY the right-hand cluster drives smooth zero-lag 125 Hz linear camera rotation and mouse clicks — no more toggling mouse mode to walk and look!"),
            ("k", "Preset A (Default): Arrows", "Smooth 125 Hz 3D camera look / menu cursor (Enter = Left Click, RShift = Right Click)"),
            ("k", "Preset B (Compact): I J K L", "Compact / 60% / CardputerZero 3D look (U = Left Click, O = Right Click)"),
            ("k", "[  /  ]", "Scroll Wheel Up / Down (cycle hotbar slots)"),
            ("k", "-  /  =", "Decrease / Increase Game Look Sensitivity live (1..10, auto-saved)"),
            ("tip", "Configure Pointer Toggle Mode (Auto / Desktop / Game), Look Preset (Arrows vs IJKL), Desktop Mouse Speed, and Game Look Sensitivity anytime in Apps -> O (Options)."),
        ],
    },
    {
        "id": "bluetooth",
        "tab": "apps",
        "tag": "BT",
        "title": "Bluetooth Manager",
        "summary": "Pair headphones, speakers, keyboards, auto-enforce A2DP Stereo & test sound.",
        "blocks": [
            ("h", "What It Is For"),
            ("p", "Connect Bluetooth headphones, speakers, keyboards, mice, and gamepads, and switch EmberJack's active audio output between Bluetooth, HDMI 0/1, 3.5mm Headphones, and USB audio — all without a desktop."),
            ("h", "Pairing Headphones Step-by-Step"),
            ("b", "1. Put your headphones or speaker into pairing mode (blinking LED)."),
            ("b", "2. Open Bluetooth from the Main Menu and press S or F1 to scan (8 seconds)."),
            ("b", "3. Highlight your headphones (marked HP or SPK) and press Enter -> Pair & Connect."),
            ("b", "4. EmberJack automatically pairs, confirms passkeys, trusts for auto-reconnect, enforces high-fidelity A2DP Stereo (never muffled HFP/HSP mono), and routes system audio to them."),
            ("b", "5. Press T to play a gentle two-tone Test Audio Chime to confirm sound."),
            ("h", "Controls"),
            ("k", "S  or  F1", "Scan for nearby Bluetooth devices (8s)"),
            ("k", "Enter", "Open device actions (Connect, Disconnect, Route Audio, Trust, Forget)"),
            ("k", "Left / Right / Tab / 1-3", "Switch tabs: ALL, AUDIO (headphones/speakers), PAIRED"),
            ("k", "O  or  F2", "Tools menu: Switch Audio Output, Play Test Chime, Power ON/OFF"),
            ("k", "T", "Play stereo Test Audio Chime immediately"),
            ("k", "+ / -", "Raise / Lower output volume by 5%"),
            ("k", "P / R", "Toggle Bluetooth adapter Power / Refresh device list"),
            ("k", "Esc / Backspace / Q", "Exit Bluetooth Manager"),
            ("tip", "For full mixer control, per-app volumes, or fixing muffled Headset-only BT audio, open Audio from the Main Menu (Tools -> Fix BT A2DP Stereo)."),
        ],
    },
    {
        "id": "audio_manager",
        "tab": "apps",
        "tag": "AUDIO",
        "title": "Audio & Volume Manager",
        "summary": "Manage outputs, microphones, per-app streams, BT A2DP profiles & L/R tests.",
        "blocks": [
            ("h", "What It Is For"),
            ("p", "Open Audio from the Main Menu for a full Obsidian Ember mixer — no desktop needed. Control HDMI 0/1, 3.5mm Headphones, USB audio, and Bluetooth outputs, adjust microphone gain, mix active per-app playback streams (pavucontrol-style), switch Bluetooth A2DP vs Headset profiles, fix bluealsa A2DP conflicts, and run L/R stereo channel tests."),
            ("h", "The 4 Tabs (Tab or Keys 1-4)"),
            ("b", "1 OUTPUTS: All playback outputs (Bluetooth, HDMI, 3.5mm, USB). Highlight a device and press Enter to make it default (auto-selects A2DP Stereo on BT!), Left/Right (-/+) for live volume, M to mute."),
            ("b", "2 INPUTS: Microphones and capture devices. Adjust input level, mute mic, or press Enter to set default capture source."),
            ("b", "3 STREAMS: Live per-app audio mixer for currently playing apps (YouTube, YT Music, Firefox, etc.). Adjust individual app volumes, mute, or press Enter to move an app to a different speaker/headphone."),
            ("b", "4 TOOLS: Stereo / Left-only / Right-only channel test tones, Sound Card / Bluetooth Profile switcher (A2DP Stereo vs HFP/HSP), Fix BT A2DP Stereo (Unblock) to stop/mask conflicting bluealsa, quick jump to Bluetooth Manager, and PipeWire / WirePlumber service restart."),
            ("h", "Controls"),
            ("k", "Tab / 1-4 / [ ]", "Switch between Outputs, Inputs, Streams, and Tools"),
            ("k", "Up / Down", "Select device, microphone, app stream, or tool"),
            ("k", "Left / Right  or  - / +", "Adjust volume (-5% / +5%) live"),
            ("k", "Enter / OK", "Set default output/mic, move stream to output, or run tool"),
            ("k", "M  or  F1", "Mute / Unmute highlighted device or stream"),
            ("k", "T  or  F2", "Play stereo test chime immediately"),
            ("k", "B / R", "Open Bluetooth Manager / Refresh all audio devices"),
            ("k", "Esc / Backspace / Q", "Exit Audio Manager"),
            ("tip", "Works seamlessly under both desktop PipeWire/PulseAudio and headless root Pi services (automatically connects to your user PipeWire/WirePlumber session, with direct ALSA fallback)."),
        ],
    },
    {
        "id": "youtube_video",
        "tab": "apps",
        "tag": "VIDEO",
        "title": "YouTube Video Player",
        "summary": "Smooth short & multi-hour YouTube with Playlists, Subs, Chapters, CC & Speed.",
        "blocks": [
            ("h", "Where To Find It"),
            ("p", "Open Payload -> utilities -> youtube. Streams both short clips (RAM-preloaded for instant replay) and multi-hour long videos (low-RAM seekable anti-throttle HTTP Range proxy using ~6 MB RAM, or optional mpv GPU/DRM engine), with real-name playlists, batch playlist downloads, Subscriptions, Chapters, Closed Captions, Playback Speed, and silent auto-resume."),
            ("h", "Home Menu & Browsing"),
            ("b", "Search / URL, Music Mode, Subscriptions (followed channels + latest feed), Playlists & Later (Watch Later + custom/imported playlists), Quick Search presets, Liked Videos, Downloads (with playlist folders), and Settings & Audio."),
            ("k", "Enter", "Play highlighted video (prompts to resume if stopped mid-video)"),
            ("k", "M  or  F1", "Play audio-only (Music Mode)"),
            ("k", "W / A / L", "Toggle Watch Later / Add to Playlist (or Save Playlist) / Like"),
            ("k", "F / U / V", "Filter Search (or Follow Channel) / Browse Channel / Video Info Card"),
            ("k", "D", "Download video (MP4/MP3) — or Download ENTIRE Playlist into a folder"),
            ("k", "S  or  F2", "Quality, Render Scale (Auto/Balanced/Fast), Speed, Captions, Engine & Audio"),
            ("h", "During Video Playback"),
            ("k", "Space / Enter / P", "Pause / Resume"),
            ("k", "Left / Right / PgUp / PgDn", "Seek -15s / +15s  or  -60s / +60s"),
            ("k", "0 - 9", "Jump directly to 0% - 90% of video duration"),
            ("k", "C  or  < / >", "Open Chapter Picker / Jump Previous or Next Chapter"),
            ("k", "T", "Toggle English Closed Captions (WebVTT subtitles)"),
            ("k", "Z / X  (or [ / ])", "Decrease / Increase Playback Speed (0.5x - 2.0x)"),
            ("k", ",  /  .", "Live A/V Sync adjustment (-50ms / +50ms) for Bluetooth lip-sync"),
            ("k", "R / K / V", "Toggle Loop Video / Save Frame Screenshot / Video Info & Description"),
            ("k", "W / A / F / L / D", "Watch Later / Add to Playlist / Follow Channel / Like / Download"),
            ("k", "Up / Down  or  + / -", "Volume Up / Down (5% steps); M to Mute; I for OSD"),
            ("k", "N / B / S", "Next / Previous playlist video; S to switch Quality (keeps timestamp)"),
            ("k", "Esc / Backspace / Q", "Stop playback (silently remembers resume timestamp)"),
            ("tip", "Long videos (>15 min) and mid-video seeks automatically engage the STREAM anti-throttle Range proxy (~6 MB RAM, zero /dev/shm bloat). For 720p/1080p on Pi, try Settings -> Player Engine -> mpv Hardware (GPU/DRM)!"),
        ],
    },
    {
        "id": "youtube_music",
        "tab": "apps",
        "tag": "MUSIC",
        "title": "YouTube Music Player",
        "summary": "Offline-first music player with real-name playlists, batch DL, EQ & visualizers.",
        "blocks": [
            ("h", "Where To Find It"),
            ("p", "Open Payload -> utilities -> youtube_music. Continuous 48kHz background audio with instant pause, glitch-free Bluetooth/HDMI switching, real-name playlist importing & syncing, full-playlist offline MP3 downloading, offline folder/playlist organization, 4 FFT visualizers, synced lyrics, 9 EQ presets, Loudness Normalizer, Playback Speed, and Sleep Timer."),
            ("h", "The 6 Tabs (Tab or Keys 1-6)"),
            ("b", "1 NOW: Album art, 4 visualizer modes (V: Peak VU / Bars / Mirror / Cover) or synced lyrics (Y), speed/EQ/sleep pills."),
            ("b", "2 SEARCH: Search songs/artists/albums or paste any YouTube / YT Music playlist URL (press S to save results as a playlist)."),
            ("b", "3 RADIO: 10 curated instant-play genre & mood stations (Lo-Fi, Synthwave, Indie, Jazz, Rock, Electronic, etc.)."),
            ("b", "4 QUEUE: Live Up Next queue — jump to tracks, reorder with [ ], remove (X), or Save Queue as Playlist (S)."),
            ("b", "5 LISTS: Liked Songs, Most Played, and Custom / Imported Playlists (auto-named after real YouTube title!). Press D on a playlist to download ALL songs offline, or S for Rename / Sync / Sort / Shuffle / Export M3U."),
            ("b", "6 OFFLINE: Browse All Offline Songs or Playlist Folders (F), Sort (S), Multi-Select with V, and press A to organize songs into playlists or move files into folders."),
            ("h", "Key Controls (Work Across All Tabs)"),
            ("k", "Space / P", "Instant Pause / Resume (<20ms sink teardown & clean reopen)"),
            ("k", "N / B", "Next / Previous track (B restarts track if >4s in)"),
            ("k", "Left / Right / , / . (NOW)", "Seek -15s / +15s;  , / . adjusts Playback Speed (0.75x - 2.0x)"),
            ("k", "Up / Down or +/-", "Volume Up / Down; M to Mute"),
            ("k", "/  or  F1", "Search YouTube Music or paste track/playlist URL"),
            ("k", "Y / V / E / G", "Toggle Lyrics / Cycle Visualizer / Cycle 9 EQs / Start Auto-Radio Mix"),
            ("k", "Q / W / A / L / D", "Add to Queue / Play Next / Organize to Playlist or Folder / Like / Download"),
            ("k", "F2 / O", "Settings: EQ, Speed, Loudness Normalizer, Sleep Timer, Audio Output, Audio Manager & Bluetooth"),
            ("k", "Esc / Backspace", "Clear multi-selection, close open playlist, or exit YT Music"),
        ],
    },

    # =========================================================================
    # TAB: NET — Main Menu Security & Network Tools
    # =========================================================================
    {
        "id": "scan_nmap",
        "tab": "net",
        "tag": "RECON",
        "title": "Scan Nmap (Network Discovery)",
        "summary": "Built-in Nmap scan profiles for discovering hosts, ports, and vulns.",
        "blocks": [
            ("h", "What It Is For"),
            ("p", "Scan Nmap automates network reconnaissance on your connected LAN subnet using Nmap. It detects your active network interface and CIDR automatically, runs the selected scan profile, and saves readable reports to loot/Nmap/."),
            ("h", "Included Scan Profiles"),
            ("b", "Quick Scan / Top100 Scan: Fast check of the most common open TCP ports on live hosts."),
            ("b", "Ping Sweep: Quickly lists every active IP address on the subnet without port scanning."),
            ("b", "Service Scan / OS Scan: Fingerprints running software versions and operating systems."),
            ("b", "Full Port Scan / Intensive Scan: Deep scan across all 65,535 TCP ports with scripts."),
            ("b", "Stealth SYN Scan / UDP Scan: Half-open SYN discovery and common UDP services (DNS, SNMP, NTP)."),
            ("b", "Vulnerability / Full Vulns / HTTP Enumeration: Runs Nmap NSE vulnerability and web enumeration scripts."),
            ("h", "Viewing Results"),
            ("p", "After a scan finishes, open Main Menu -> Read file -> Nmap to browse and read saved scan outputs with PageUp/PageDown."),
            ("warn", "Only run active port and vulnerability scans on networks you own or have explicit written authorization to test."),
        ],
    },
    {
        "id": "wifi_manager",
        "tab": "net",
        "tag": "WIFI",
        "title": "WiFi Manager & Dual Interfaces",
        "summary": "Connect to WiFi, switch wlan0/wlan1, routing, and WebUI access.",
        "blocks": [
            ("h", "What It Is For"),
            ("p", "EmberJack supports dual WiFi interfaces: onboard Pi WiFi (wlan0) for management/internet and an external USB monitor-mode dongle (wlan1) for wireless auditing. WiFi Manager lets you connect, switch, inspect, and control routing."),
            ("h", "Menu Options"),
            ("b", "Full WiFi Manager: Scan SSIDs, enter passwords, connect, and manage saved profiles."),
            ("b", "FAST WiFi Switcher / INSTANT Toggle 0<->1: Rapidly swap your active WiFi interface."),
            ("b", "Switch Interface / Show Interface Info: Pick active adapter and view IP, MAC, signal, and mode."),
            ("b", "Route Control: Inspect and adjust default gateway / routing table when multiple links are up."),
            ("b", "WebUI: Displays WebUI URL/status if you ever want to control EmberJack from a browser."),
            ("tip", "If an attack payload needs monitor mode (deauth, handshake capture, evil twin), plug in a monitor-capable USB WiFi adapter (Realtek/Atheros/Mediatek) so wlan0 stays connected."),
        ],
    },
    {
        "id": "mitm_responder_dns",
        "tab": "net",
        "tag": "LAN",
        "title": "MITM, Responder & DNS Spoof",
        "summary": "Local network traffic interception, credential capture & DNS redirect.",
        "blocks": [
            ("h", "MITM & Sniff (ARP Spoofing)"),
            ("p", "Positions EmberJack between target LAN hosts and the router via ARP spoofing (arpspoof + tcpdump/ettercap) to inspect traffic in transit. Select Start MITM & Sniff, pick your target subnet, and always run Stop MITM & Sniff when finished to restore ARP tables."),
            ("h", "Responder (LLMNR / NBT-NS / MDNS)"),
            ("p", "Listens for Windows/LAN name-resolution fallback broadcasts (LLMNR, NetBIOS, mDNS) and captures NTLMv2 challenge-response hashes for offline password auditing. Toggle Responder ON / Responder OFF and inspect captures under Main Menu -> Read file -> Responder logs."),
            ("h", "DNS Spoofing & Local Sites"),
            ("p", "Redirects DNS lookups on the intercepted network to a locally hosted PHP page served by EmberJack. First pick a template under DNS Spoofing -> Select site, then Start DNSSpoofing, and use Stop DNS&PHP when done. Logs appear in Read file -> DNSSpoof."),
            ("warn", "Authorized lab & penetration-testing use ONLY. Always stop MITM/DNS/Responder cleanly so target devices regain normal connectivity."),
        ],
    },
    {
        "id": "reverse_shell_sys",
        "tab": "net",
        "tag": "SYS",
        "title": "Reverse Shell, Info, Files & Lock",
        "summary": "Remote shells, Network Info, Read File, Themes, Clock & Lock Screen.",
        "blocks": [
            ("h", "Reverse Shell"),
            ("p", "Opens an outbound shell connection back to a configured listener IP/port (Defaut Reverse uses your saved listener; Remote Reverse lets you specify an endpoint). Useful for lab red-team persistence exercises."),
            ("h", "Network Info & Read File"),
            ("b", "Network Info: Shows live interfaces, IPv4 addresses, gateway, SSID, and connectivity status."),
            ("b", "Read File: Built-in scrollable viewer for Nmap scans, Responder logs, Wardriving logs, and DNSSpoof logs."),
            ("h", "Other Features -> Options & System"),
            ("b", "Colors -> Theme Presets: Switch between Obsidian Ember (default), Deep Amber, Carbon Copper, and Classic Green — or customize individual RGB channels."),
            ("b", "Flip Screen 180 / Clock Settings: Rotate display, set timezone, toggle NTP sync, or hide clock."),
            ("b", "Browse Images: Inspect screenshots and saved images on-device."),
            ("b", "System: Cleanly Restart UI or Shutdown system safely (always shutdown cleanly before pulling Pi power!)."),
            ("h", "Lock Screen"),
            ("p", "Protect physical access with a 4-digit PIN or 6-step button sequence, configure auto-lock timeout, and optionally display a custom screensaver GIF."),
        ],
    },

    # =========================================================================
    # TAB: TIPS — Pi Setup, CLI Scripts, Loot & Troubleshooting
    # =========================================================================
    {
        "id": "cli_and_setup_scripts",
        "tab": "tips",
        "tag": "CLI",
        "title": "CLI Launchers & Setup Scripts",
        "summary": "Brief guide to run_hdmi.py, install_raspyjack.sh & optional scripts/.",
        "blocks": [
            ("h", "Core Launch & Installer Commands"),
            ("k", "run_hdmi.py", "Launch EmberJack interactively in HDMI mode (--no-root, --fullscreen, --backend)."),
            ("k", "install_raspyjack.sh", "Full bootstrap & updater (--hdmi, --update, --no-service, -y). Installs deps, PipeWire BT audio, cage/seatd & services."),
            ("h", "Optional Setup Scripts (in scripts/)"),
            ("p", "Run these from Terminal or SSH only if you use the matching optional hardware or offline data features:"),
            ("k", "install_proxmark3.sh", "Install Proxmark3 client & drivers for PM3 NFC/RFID payloads."),
            ("k", "install_pyboy.sh", "Install PyBoy so the Game Boy emulator (boy_emulator) can run ROMs."),
            ("k", "install_ragnar_port.sh", "Install dependencies for the vendored Ragnar autonomous scanner."),
            ("k", "download_tiles.sh", "Pre-download offline OpenStreetMap tiles for map_viewer & wardriving."),
            ("k", "build_aircraft_db.sh", "Build offline aircraft/airline lookup database for sdr_adsb."),
            ("k", "build_geoip_db.sh", "Build offline GeoIP database for network & recon tools."),
            ("k", "update_noaa_tle.sh", "Refresh NOAA/Meteor satellite TLE orbital data for sdr_meteor & iss_tracker."),
            ("k", "optimize_gifs.py", "Optimize screensaver GIFs for smooth low-CPU playback on the Pi."),
        ],
    },
    {
        "id": "loot_and_cooling",
        "tab": "tips",
        "tag": "PI",
        "title": "Loot Folders, Updates & Cooling",
        "summary": "Where files are saved, thermal efficiency tips & updating via scp.",
        "blocks": [
            ("h", "Where Your Files Live"),
            ("b", "loot/Nmap/ — Saved Nmap scan outputs (viewable in Read file)."),
            ("b", "loot/YouTube/Downloads/ — Downloaded MP4 videos, MP3 audio & playlist folders (plus playlists.json & subscriptions.json in loot/YouTube/)."),
            ("b", "loot/YTMusic/Library/ — Offline MP3 tracks organized by playlist folder, exported .m3u lists & cached lyrics."),
            ("b", "loot/Apps/ — Custom GUI app entries, favorites, and Mouse Speed settings."),
            ("b", "loot/screenshots/ — Screenshots captured with F12 and YouTube frame captures (K)."),
            ("h", "Keeping Your Raspberry Pi Cool"),
            ("b", "EmberJack is event-driven when idle (~0% background redraw CPU) and skips WebUI JPEG encoding automatically when no browser is connected."),
            ("b", "Use 360p (1:1 640x360 H.264) or 720p (fast avc1 H.264), or switch to mpv Hardware (GPU/DRM) for lowest CPU load on long videos."),
            ("b", "Check live CPU temperature in the top-right status bar at any time."),
            ("h", "Updating EmberJack"),
            ("p", "You can update individual files via scp into your EmberJack folder and run 'sudo systemctl restart raspyjack.service', or run './install_raspyjack.sh --update --hdmi' to refresh packages, yt-dlp, Bluetooth A2DP config, and services safely."),
        ],
    },
    {
        "id": "troubleshooting",
        "tab": "tips",
        "tag": "FIX",
        "title": "Audio, Bluetooth, YouTube & App Fixes",
        "summary": "Quick fixes for muffled BT audio, slow long videos, sound, or GUI apps.",
        "blocks": [
            ("h", "Muffled / Low-Quality Bluetooth Audio (Headset Head Unit Only)"),
            ("b", "1. Cause: bluealsa (bluez-alsa-utils) steals BlueZ's A2DP Stereo endpoints before PipeWire starts, leaving only tinny 16kHz mono Headset (HFP/HSP) profiles."),
            ("b", "2. One-click fix inside EmberJack: Open Main Menu -> Audio -> Tools (Tab 4) -> Fix BT A2DP Stereo (Unblock), then reconnect your headphones."),
            ("b", "3. Permanent installer fix: Run './install_raspyjack.sh --update --hdmi' — it masks bluealsa automatically and configures WirePlumber A2DP Stereo."),
            ("h", "No Sound / Bluetooth Won't Connect (Headless Pi)"),
            ("b", "1. Open Audio from the Main Menu to pick your active output (Bluetooth, HDMI 0/1, 3.5mm, USB), check mute/volume, run Left/Right channel tests, or use Tools -> Restart Audio Services."),
            ("b", "2. Make sure PipeWire & headless Bluetooth A2DP packages are installed: pipewire-audio, wireplumber, libspa-0.2-bluetooth, pulseaudio-utils (WITHOUT bluez-alsa-utils on HDMI)."),
            ("b", "3. Ensure your normal user (UID 1000) has lingering enabled (loginctl enable-linger <user>) and WirePlumber seat-monitoring disabled in /etc/wireplumber/ (done automatically by install_raspyjack.sh)."),
            ("b", "4. Verify Bluetooth audio profile with: bluetoothctl show | grep -i Audio"),
            ("h", "Slow / Stalling Long YouTube Videos or Lip-Sync Delay"),
            ("b", "1. Keep yt-dlp up to date (old Debian apt versions get throttled by YouTube): run './install_raspyjack.sh --update --hdmi' or 'sudo pip3 install --upgrade --break-system-packages \"yt-dlp[default]\"' with nodejs installed."),
            ("b", "2. Long videos (>15 min) automatically stream through the O(1)-RAM anti-throttle Range proxy (shown as STREAM on OSD) so /dev/shm never fills up."),
            ("b", "3. For 720p/1080p multi-hour videos on Pi, try YouTube -> Settings & Audio -> Player Engine -> mpv Hardware (GPU/DRM), or Render Quality -> Balanced (480x270) / Fast (320x180)."),
            ("b", "4. Audio/video lip-sync off? Press ',' (-50ms) or '.' (+50ms) live during playback, or adjust BT A/V Delay Comp in Settings."),
            ("h", "GUI App Launcher Won't Start an App / Pointer Lock Error"),
            ("b", "1. Check that 'cage', 'labwc', 'xwayland', and 'seatd' are installed and 'seatd.service' is enabled."),
            ("b", "2. If a 3D game (e.g. Minecraft Pi Reborn) says pointer locking is unsupported, open Apps -> O -> Launch Backend -> Labwc (Wayland) (or Gamescope). Auto mode automatically picks Labwc for known 3D games once installed."),
            ("b", "3. Ensure your UID-1000 user is in groups: video, render, input, seat, audio, bluetooth (reboot after adding groups)."),
            ("b", "4. Read the exact failure log anytime in Terminal: cat /dev/shm/rj_app_launcher.log"),
            ("b", "5. Stuck inside an app? Press Ctrl+Alt+Q (or Ctrl+Alt+Backspace) to return to EmberJack immediately."),
        ],
    },
]


def _build_payload_catalog_topics() -> List[dict]:
    """Generate one dedicated encyclopedia chapter per payload category from rj_tool_catalog."""
    # Logical browsing order for users (most used & security categories grouped cleanly)
    order = (
        "utilities",
        "reconnaissance",
        "wifi",
        "network",
        "credentials",
        "bluetooth",
        "nfc_rfid",
        "sdr",
        "usb",
        "exfiltration",
        "remote_access",
        "evasion",
        "hardware",
        "ai",
        "games",
    )
    ordered_cats = [c for c in order if c in rj_tool_catalog.TOOL_CATALOG]
    for c in sorted(rj_tool_catalog.TOOL_CATALOG.keys()):
        if c not in ordered_cats:
            ordered_cats.append(c)

    topics: List[dict] = []
    for cat in ordered_cats:
        items = rj_tool_catalog.TOOL_CATALOG[cat]
        disp_title, _short_tag, intro = rj_tool_catalog.CATEGORY_META.get(
            cat, (cat.replace("_", " ").title(), cat.upper()[:4], f"Payloads inside Payload -> {cat}.")
        )
        blocks: List[Block] = [
            ("h", f"Location: Payload -> {cat} ({len(items)} tools)"),
            ("p", intro),
            ("tip", "Press / to search any tool by name and jump straight to it, or type its name on the Main Menu to launch it directly."),
            ("h", f"All {len(items)} Tools in {disp_title}"),
        ]
        for tool in items:
            blocks.append((
                "tool",
                tool["id"],
                tool["title"],
                tool["desc"],
                tuple(tool.get("hw") or ()),
            ))
        topics.append({
            "id": f"cat_{cat}",
            "tab": "payloads",
            "tag": str(len(items)),
            "title": disp_title,
            "summary": f"All {len(items)} tools in Payload -> {cat}",
            "blocks": blocks,
        })
    return topics


_ALL_TOPICS_CACHE: List[dict] = []


def get_all_topics() -> List[dict]:
    """Return all structured EmberJack Guide topics (core chapters + full 328-tool encyclopedia)."""
    global _ALL_TOPICS_CACHE
    if not _ALL_TOPICS_CACHE:
        payload_topics = _build_payload_catalog_topics()
        # Insert payload encyclopedia chapters cleanly between NET and TIPS
        before_tips = [t for t in GUIDE_TOPICS if t.get("tab") != "tips"]
        tips_topics = [t for t in GUIDE_TOPICS if t.get("tab") == "tips"]
        _ALL_TOPICS_CACHE = before_tips + payload_topics + tips_topics
    return list(_ALL_TOPICS_CACHE)


def filter_topics(topics: List[dict], tab: str = "all", query: str = "") -> List[dict]:
    """Filter guide topics by category tab and optional full-text search query.

    When *query* is provided, ranks topics with direct title/tool-ID matches first
    so searching any tool name (e.g. 'kerberoast' or 'winamp') highlights its chapter immediately.
    """
    q = (query or "").strip().lower()
    scored: List[Tuple[int, int, dict]] = []
    for idx, t in enumerate(topics):
        if tab != "all" and t.get("tab") != tab:
            continue
        if not q:
            scored.append((0, idx, t))
            continue

        title_l = str(t.get("title", "")).lower()
        summary_l = str(t.get("summary", "")).lower()
        tag_l = str(t.get("tag", "")).lower()
        rank = 99
        if q in title_l:
            rank = 0
        elif q in summary_l or q in tag_l:
            rank = 2

        matched_body = (rank < 99)
        for blk in t.get("blocks", ()):
            kind = blk[0]
            if kind == "tool":
                tid = str(blk[1]).lower()
                ttitle = str(blk[2]).lower()
                tdesc = str(blk[3]).lower()
                thw = " ".join(str(x).lower() for x in (blk[4] if len(blk) > 4 else ()))
                if q == tid or q in tid or q in ttitle:
                    rank = min(rank, 1)
                    matched_body = True
                elif q in tdesc or q in thw:
                    rank = min(rank, 3)
                    matched_body = True
            else:
                blob = " ".join(str(x).lower() for x in blk[1:])
                if q in blob:
                    rank = min(rank, 3)
                    matched_body = True
        if matched_body:
            scored.append((rank, idx, t))

    scored.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in scored]
