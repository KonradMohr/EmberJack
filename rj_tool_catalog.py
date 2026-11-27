#!/usr/bin/env python3
"""
EmberJack Complete User-Facing Payload Encyclopedia Data
--------------------------------------------------------
Static, zero-overhead catalog of all 329 user-facing payloads across all 15
active categories (excluding backend/helper modules and internal test stubs).
Imported by `rj_guide.py` to power the on-device interactive Guide app.
"""

from typing import Dict, List, Tuple

# category_key -> (Display Title, Short Badge Tag, Category Intro)
CATEGORY_META: Dict[str, Tuple[str, str, str]] = {'ai': ('AI & Machine Learning',
        'AI',
        'On-device audio, vision, and traffic machine-learning tools (Payload -> ai).'),
 'bluetooth': ('Bluetooth & BLE Auditing',
               'BT',
               'Bluetooth Classic & Low Energy scanning, GATT analysis, and wireless testing (Payload -> '
               'bluetooth).'),
 'credentials': ('Credentials & Hash Auditing',
                 'CRED',
                 'Cleartext capture, default credential checks, Kerberos/NTLM/WPA auditing (Payload -> '
                 'credentials).'),
 'evasion': ('Evasion & Stealth',
             'EVADE',
             'MAC randomization, fingerprint spoofing, traffic shaping, and log cleanup (Payload -> '
             'evasion).'),
 'exfiltration': ('Loot Exfiltration',
                  'EXFIL',
                  'Export captured loot over USB, Discord, Dropbox, FTP, SMB, HTTP, DNS, ICMP, or BLE '
                  '(Payload -> exfiltration).'),
 'games': ('Retro Games & Emulators',
           'GAMES',
           '33 playable arcade, puzzle, strategy, and classic games right on your monitor (Payload -> '
           'games).'),
 'hardware': ('Hardware, GPS & Sensors',
              'HW',
              'GPS/GNSS receivers, I2C bus scanning, GPIO tripwire, LTE modems, camera & IMU tools '
              '(Payload -> hardware).'),
 'network': ('Network Protocols & MITM',
             'NET',
             'Layer-2/3 auditing: ARP/DHCP/DNS/IPv6/VLAN/STP testing, transparent bridges & traffic '
             'analysis (Payload -> network).'),
 'nfc_rfid': ('NFC & RFID Tools',
              'NFC',
              'Card reading, cloning, key cracking, and emulation for Proxmark3, Chameleon Ultra & PN532 '
              '(Payload -> nfc_rfid).'),
 'reconnaissance': ('Reconnaissance & OSINT',
                    'RECON',
                    '52 discovery tools for LANs, Active Directory, cameras, wireless clients, '
                    'certificates & OSINT (Payload -> reconnaissance).'),
 'remote_access': ('Remote Access & Tunneling',
                   'RMT',
                   'Reverse shells, persistent SSH tunnels, SOCKS5 pivots, port forwarding, Discord C2 & '
                   'Tailscale (Payload -> remote_access).'),
 'sdr': ('SDR & Sub-GHz Radio',
         'SDR',
         'RTL-SDR and CC1101 radio tools: ADS-B aircraft, APRS, weather satellites, pagers & ISM replay '
         '(Payload -> sdr).'),
 'usb': ('USB & BadUSB Gadgets',
         'USB',
         'USB HID DuckyScript injection, BadUSB detection, USB Ethernet MITM & mass-storage gadgets '
         '(Payload -> usb).'),
 'utilities': ('Utilities & Everyday Apps',
               'UTIL',
               '81 everyday utilities: Audio Manager, YouTube, YT Music, App Launcher, Bluetooth, speed tests, monitors, '
               'maps & tools (Payload -> utilities).'),
 'wifi': ('WiFi Wireless Auditing',
          'WIFI',
          '19 wireless security tools: handshakes, PMKID, Evil Twin, Captive Portal, Karma, WPS & '
          'Pwnagotchi (Payload -> wifi).')}

# category_key -> list of {'id', 'title', 'desc', 'hw'}
TOOL_CATALOG: Dict[str, List[dict]] = {'ai': [{'desc': 'Real-time bird species detection using BirdNET AI model and the ES8389 built-in '
                 'microphone.',
         'hw': ['Mic'],
         'id': 'birdnet',
         'title': 'BirdNET Live'},
        {'desc': 'ML-based network traffic anomaly detection. Learns normal traffic patterns, then alerts '
                 'on anomalies. Uses Isolation Forest (sklearn) for unsupervised detection.',
         'hw': [],
         'id': 'network_anomaly',
         'title': 'Network Anomaly Detector'},
        {'desc': 'Real-time object detection using MobileNet SSD TFLite. Detects 90 COCO classes: persons, '
                 'cars, animals, etc.',
         'hw': ['Camera'],
         'id': 'object_detector',
         'title': 'Object Detector'},
        {'desc': 'Offline speech recognition using Vosk. Records from microphone and transcribes in '
                 'real-time. Supports multiple languages.',
         'hw': ['Mic'],
         'id': 'speech_to_text',
         'title': 'Speech to Text'},
        {'desc': 'Offline voice-to-text recorder for journaling, blogging, and notes.',
         'hw': ['Mic'],
         'id': 'voice_journal',
         'title': 'Voice Journal'}],
 'bluetooth': [{'desc': 'Flood nearby Bluetooth scans with fake devices. Two modes: PRESET Named devices '
                        '(real products, hacker memes, pop culture) ENTROPY Random unicode names (letters, '
                        'digits, specials, emojis)',
                'hw': [],
                'id': 'ble_beacon_flood',
                'title': 'BLE Flood'},
               {'desc': 'Scan BLE devices, select a target, connect and enumerate all GATT '
                        'services/characteristics. Set up the Pi as a BLE peripheral advertising the same '
                        'services.',
                'hw': [],
                'id': 'ble_mitm',
                'title': 'BLE GATT MITM Proxy'},
               {'desc': 'Scan for BLE devices, connect to a target, enumerate GATT characteristics, record '
                        'notifications/reads, and replay recorded values back to the device. Setup / '
                        'Prerequisites: Requires Bluetooth adapter.',
                'hw': [],
                'id': 'ble_replay',
                'title': 'BLE GATT Replay Attack'},
               {'desc': 'Scans for BLE devices using hcitool lescan and tracks addresses, names, RSSI, '
                        'first/last seen timestamps and seen count. Provides a scrollable device list with '
                        'detail view.',
                'hw': [],
                'id': 'ble_scanner',
                'title': 'Continuous BLE Scanner Dashboard'},
               {'desc': 'Broadcast crafted BLE advertisements to trigger popup notifications on nearby '
                        'devices. Setup / Prerequisites: Requires Bluetooth adapter (hci0). Targets: iOS '
                        '(Apple Proximity), Android (Google FastPair), Windows (Swift Pair).',
                'hw': [],
                'id': 'ble_spam',
                'title': 'BLE Spam (iOS / Android / Windows)'},
               {'desc': 'Scan for Bluetooth A2DP devices (speakers, headphones), attempt pairing, and play '
                        'audio through the target device.',
                'hw': [],
                'id': 'bt_audio_inject',
                'title': 'Bluetooth Audio Injection'},
               {'desc': 'Scan for Bluetooth Classic devices, select a target, and send continuous L2CAP '
                        "ping requests to stress the target's Bluetooth stack. Uses l2ping with "
                        'configurable packet size.',
                'hw': [],
                'id': 'bt_dos',
                'title': 'Bluetooth L2CAP Ping Flood'},
               {'desc': 'Detects smart glasses via BLE (Meta Ray-Ban, Snap Spectacles, etc.) then offers '
                        'counter-attack modes to disrupt them via BLE flood/spam.',
                'hw': [],
                'id': 'glasses_counter',
                'title': 'Smart Glasses Counter-Attack'},
               {'desc': 'Demonstrates the Fast Pair pairing mode bypass vulnerability. Scans for '
                        'vulnerable Bluetooth audio devices and tests if they accept pairing requests '
                        'outside of pairing mode. AUTHORIZED TESTING ONLY — only test devices you own.',
                'hw': [],
                'id': 'whisperpair',
                'title': 'WhisperPair (CVE-2025-36911)'}],
 'credentials': [{'desc': 'Passive credential sniffer using Scapy. Captures cleartext and encoded '
                          'credentials from multiple protocols: FTP (21), Telnet (23), SMTP (25), HTTP '
                          '(80), Kerberos (88), POP3 (110), IMAP (143), LDAP (389), SMB/NTLM (445) Loot.',
                  'hw': [],
                  'id': 'cred_sniffer_multi',
                  'title': 'Multi-Protocol Credential Sniffer'},
                 {'desc': 'Auto-discovers hosts via ARP scan, then probes common services (SSH, FTP, '
                          'Telnet, HTTP, SNMP, MySQL) with built-in default creds. Prerequisites: sshpass, '
                          'scapy, requests Loot: /root/Raspyjack/loot/DefaultCreds/creds_TIMESTAMP.json.',
                  'hw': [],
                  'id': 'default_creds_scanner',
                  'title': 'Default Credentials Scanner'},
                 {'desc': 'Auto-discovers FTP hosts from nmap loot or quick port-21 scan on the local '
                          'subnet, then sprays ~50 common user:pass pairs against each host using ftplib '
                          '(stdlib).',
                  'hw': [],
                  'id': 'ftp_bruteforce',
                  'title': 'FTP Credential Brute-Force'},
                 {'desc': 'Runs during active MITM. Uses scapy to sniff TCP port 80 traffic and extracts '
                          'HTTP Basic Auth headers, POST form data with credential fields, and Set-Cookie '
                          'headers. Loot: /root/Raspyjack/loot/HTTPCreds/http_creds_YYYYMMDD_HHMMSS.json.',
                  'hw': [],
                  'id': 'http_cred_sniffer',
                  'title': 'Passive HTTP Credential Extractor'},
                 {'desc': 'Kerberoasting / AS-REP Roasting via impacket (GetUserSPNs.py, GetNPUsers.py). '
                          'Auto-detects DC, collects TGS/AS-REP hashes in hashcat format. Loot: '
                          '/root/Raspyjack/loot/Kerberoast/.',
                  'hw': [],
                  'id': 'kerberoast',
                  'title': 'Kerberoast & AS-REP Roast'},
                 {'desc': 'Cracks captured NTLM hashes using John the Ripper. Scans Responder logs and '
                          'NTLMRelay loot for hash files, then runs john with user-selected attack mode.',
                  'hw': [],
                  'id': 'ntlm_cracker',
                  'title': 'NTLM Hash Cracker'},
                 {'desc': 'Wrapper around the vendored Responder tool at /root/Raspyjack/Responder/. '
                          'Captures NTLM hashes via poisoning and optionally relays them to a target host.',
                  'hw': [],
                  'id': 'ntlm_relay',
                  'title': 'NTLM Relay Attack'},
                 {'desc': 'Uses captured NTLM hashes to authenticate to other Windows machines via SMB '
                          '(port 445). Leverages smbclient --pw-nt-hash or impacket tools (psexec.py / '
                          'smbexec.py / wmiexec.py) when available.',
                  'hw': [],
                  'id': 'pass_the_hash',
                  'title': 'Pass the Hash (PtH)'},
                 {'desc': 'Discovers SNMP hosts on the local network (UDP 161), brute-forces community '
                          'strings, and walks common MIB OIDs using raw SNMPv1/v2c packets via scapy. '
                          'Loot: /root/Raspyjack/loot/SNMP/snmp_YYYYMMDD_HHMMSS.json.',
                  'hw': [],
                  'id': 'snmp_walk',
                  'title': 'SNMP Community Brute-Force + MIB Walk'},
                 {'desc': 'Auto-discovers SSH hosts from nmap loot or quick port-22 scan on the local '
                          'subnet, then sprays ~50 common user:pass pairs using sshpass.',
                  'hw': [],
                  'id': 'ssh_bruteforce',
                  'title': 'SSH Credential Spray'},
                 {'desc': 'Scans for port 23 on the local subnet, grabs banners via raw socket, then tries '
                          '~30 common IoT/router default credentials. Setup / Prerequisites: Built-in '
                          'default credential list. No special requirements.',
                  'hw': [],
                  'id': 'telnet_grabber',
                  'title': 'Telnet Banner Grab & Default Cred Test'},
                 {'desc': 'Cracks WPA handshakes (.cap) using aircrack-ng and PMKID hashes using John the '
                          'Ripper. Scans loot directories for crack targets. Setup / Prerequisites: '
                          'Requires aircrack-ng for .cap handshake files.',
                  'hw': [],
                  'id': 'wpa_cracker',
                  'title': 'WPA/WPA2 Cracker'}],
 'evasion': [{'desc': 'Spoof the TCP/IP stack fingerprint via sysctl to impersonate different operating '
                      'systems. Changes: net.ipv4.ip_default_ttl, TCP window size, and DF bit behaviour. '
                      'Presets: Linux, Windows 10, macOS, Cisco IOS, Printer.',
              'hw': [],
              'id': 'fingerprint_spoof',
              'title': 'TCP/IP Fingerprint Spoofer'},
             {'desc': 'Selective cleanup of forensic artifacts after an engagement. Protects '
                      '/root/Raspyjack/loot/ (operator data).',
              'hw': [],
              'id': 'log_cleaner',
              'title': 'Engagement Log Cleaner'},
             {'desc': 'Randomize, restore, or clone MAC addresses on network interfaces.',
              'hw': [],
              'id': 'mac_randomizer',
              'title': 'MAC Address Randomizer'},
             {'desc': "One-click toggle to minimize the Pi's visible footprint: Disable ACT/PWR LEDs "
                      'Reduce WiFi TX power to minimum Randomize MAC addresses on all interfaces Change '
                      'hostname to generic name Flush system logs and bash history Disable syslog.',
              'hw': [],
              'id': 'stealth_mode',
              'title': 'Stealth Mode Toggle'},
             {'desc': 'Configures tc (traffic control) qdisc on the active network interface to add random '
                      'jitter to outgoing packets. Prevents IDS/IPS from detecting scan patterns by adding '
                      'randomised latency.',
              'hw': [],
              'id': 'timing_evasion',
              'title': 'Network Timing Randomizer'},
             {'desc': 'Uses Linux tc (traffic control) to limit and shape traffic on the bridge interface '
                      'during MITM operations, keeping latency under suspicious thresholds to maintain '
                      'stealth.',
              'hw': [],
              'id': 'traffic_shaper',
              'title': 'MITM Traffic Shaper'}],
 'exfiltration': [{'desc': 'Watches /root/Raspyjack/loot/ for new files using os.scandir polling (every '
                           '10s). When a new file is detected, exfiltrate via the configured channel: '
                           'Discord webhook, HTTP POST, or DNS tunnel.',
                   'hw': [],
                   'id': 'auto_loot_exfil',
                   'title': 'Auto Loot Exfiltration Daemon'},
                  {'desc': 'Ultra stealthy data exfiltration via BLE advertisement packets. No WiFi or '
                           'network connection required. Setup / Prerequisites: Requires Bluetooth adapter '
                           '(hci0). Receiver must scan for BLE manufacturer ID 0xFFFF to reassemble data.',
                   'hw': [],
                   'id': 'ble_exfil',
                   'title': 'BLE Advertisement Exfiltration'},
                  {'desc': 'Encodes files from the loot directory into base32 DNS subdomain labels and '
                           'sends them as TXT queries to a configurable external domain. Setup / '
                           'Prerequisites: Edit config at /root/Raspyjack/config/dns_tunnel/config.json.',
                   'hw': [],
                   'id': 'dns_tunnel',
                   'title': 'DNS Exfiltration Tunnel'},
                  {'desc': 'Upload loot files to Dropbox via the v2 API (urllib only, no external deps). '
                           'Supports browsing loot directory, selecting files/folders, chunked upload for '
                           'files >150MB.',
                   'hw': [],
                   'id': 'dropbox_upload',
                   'title': 'Dropbox Loot Uploader'},
                  {'desc': 'Lightweight FTP server serving /root/Raspyjack/loot/ read-only. Uses pyftpdlib '
                           'when available; otherwise falls back to a minimal socket-based FTP server '
                           'supporting LIST, RETR, PWD, CWD, TYPE, PASV, and QUIT.',
                   'hw': [],
                   'id': 'exfil_ftp',
                   'title': 'Mini FTP Server for Loot Retrieval'},
                  {'desc': 'Two-mode SMB exfiltration tool: Mode 1 (Serve): Start an SMB share on the Pi '
                           "serving /root/Raspyjack/loot/ using impacket-smbserver (from Responder's "
                           'vendored impacket).',
                   'hw': [],
                   'id': 'exfil_smb',
                   'title': 'SMB Exfiltration'},
                  {'desc': 'Monitors for USB mass storage insertion using pyudev (or polling /dev/sd*). '
                           'When detected: mount the drive, copy all of /root/Raspyjack/loot/ to the USB '
                           'under a timestamped folder, then unmount.',
                   'hw': [],
                   'id': 'exfil_usb',
                   'title': 'USB Drive Auto-Copy'},
                  {'desc': 'This script gathers: everything under the local loot/ folder, including the '
                           'MITM/ and Nmap/ sub-directories; every file in ./Responder/logs/ …bundles them '
                           'into a single ZIP archive and uploads it as an attachment to a Discord.',
                   'hw': [],
                   'id': 'exfiltrate_discord',
                   'title': 'Upload Loot to Discord'},
                  {'desc': 'Reads files from /root/Raspyjack/loot/, encodes in base64, and sends as POST '
                           'chunks to a configurable URL. Setup / Prerequisites: Edit config at '
                           '/root/Raspyjack/config/http_exfil/config.json with target_url of your exfil '
                           'server.',
                   'hw': [],
                   'id': 'http_exfil',
                   'title': 'HTTP Exfiltration'},
                  {'desc': 'Encodes data into ICMP echo request payloads and sends to a configurable '
                           'remote IP. Supports file transfer and optional XOR encryption. Setup / '
                           'Prerequisites: Configure target IP (the receiving machine must capture ICMP '
                           'payloads).',
                   'hw': [],
                   'id': 'icmp_tunnel',
                   'title': 'ICMP Covert Channel'},
                  {'desc': 'Generate DuckyScript payloads that establish reverse shells on targets. Select '
                           'target OS, shell type, callback IP/port via on-device menus. Saves generated '
                           'scripts to /root/Raspyjack/loot/DuckyPayloads/.',
                   'hw': [],
                   'id': 'reverse_ducky',
                   'title': 'Reverse Shell DuckyScript Generator'},
                  {'desc': 'Upload wardriving sessions to wdgwars.pl (Watch Dogs Go Wars). Supports CSV '
                           'upload (Wigle format) and profile check. Setup: Enter your 64-char API key '
                           'from your WDGoWars profile.',
                   'hw': [],
                   'id': 'wdgwars_upload',
                   'title': 'WDGoWars Upload'}],
 'games': [{'desc': 'Classic top-down arcade shooter inspired by 1980s games. Destroy descending enemies, '
                    'survive as long as you can.',
            'hw': [],
            'id': '1988',
            'title': '1988 Retro Arcade'},
           {'desc': 'Classic 4x4 sliding-number puzzle game: merge matching tiles with arrow keys to reach '
                    '2048. Press F1 for a new game.',
            'hw': [],
            'id': '2048',
            'title': '2048'},
           {'desc': 'Classic arcade Break-out clone for the Waveshare 1.44-inch LCD HAT on Raspberry Pi. '
                    'Inspired by – and building upon – the companion example example_show_buttons.py.',
            'hw': [],
            'id': 'Breakout',
            'title': 'Breakout'},
           {'desc': 'Classic Asteroids on LCD. Ship rotates, thrusts, and fires bullets at irregular '
                    'polygon asteroids that break into smaller pieces.',
            'hw': [],
            'id': 'asteroids',
            'title': 'Asteroids'},
           {'desc': 'Bomberman clone on CardputerZero LCD. Runs on Xvfb, captures via ffmpeg x11grab to '
                    'framebuffer.',
            'hw': [],
            'id': 'bomberman',
            'title': 'Bomberman (BomberClone)'},
           {'desc': 'Play Game Boy / Game Boy Color ROMs on the LCD using PyBoy. Place .gb or .gbc ROM '
                    'files in /root/Raspyjack/roms/ The emulator renders frames to the LCD at ~20 FPS on '
                    'Pi Zero 2.',
            'hw': ['PyBoy'],
            'id': 'boy_emulator',
            'title': 'Game Boy Emulator'},
           {'desc': 'Physics-based 2D platformer on CardputerZero LCD. Runs on Xvfb, captures via ffmpeg '
                    'x11grab to framebuffer.',
            'hw': [],
            'id': 'caveexpress',
            'title': 'CaveExpress'},
           {'desc': 'Connect 4 vs AI with minimax alpha-beta pruning on LCD.',
            'hw': [],
            'id': 'connect4',
            'title': 'Connect 4 (Puissance 4)'},
           {'desc': 'Interactive cellular automaton simulation. Pause with OK/Right to paint cells with '
                    'F2, randomize with F1, and watch patterns evolve.',
            'hw': [],
            'id': 'conways_game_of_life',
            'title': "Conway's Game of Life"},
           {'desc': 'Pikmin-style game in a cyberpunk network world. Control a hacker leading a swarm of '
                    'bots through servers.',
            'hw': [],
            'id': 'cyber_swarm',
            'title': 'Cyber Swarm (Pikmin-like)'},
           {'desc': 'Tilt the CardputerZero to move left/right, auto-bounce on platforms, climb as high as '
                    'you can!',
            'hw': ['IMU'],
            'id': 'doodle_jump',
            'title': 'Doodle Jump (IMU)'},
           {'desc': 'Runs chocolate-doom on Xvfb, captures frames via ffmpeg x11grab, and streams to the '
                    'LCD framebuffer. Keyboard via evdev.',
            'hw': ['Doom'],
            'id': 'doom',
            'title': 'DOOM (FreeDoom)'},
           {'desc': 'A polished tower defense game with network security theming, pixel-art sprites, '
                    'path-based enemy movement, laser effects, explosions and a detailed UI.',
            'hw': [],
            'id': 'firewall_defense',
            'title': 'Firewall Defense'},
           {'desc': 'Flappy bird on the LCD. Green-on-black theme.',
            'hw': [],
            'id': 'flappy',
            'title': 'Flappy Bird Clone'},
           {'desc': 'Classic arcade road-and-river crossing game. Dodge traffic and hop across moving logs '
                    'to reach the top.',
            'hw': [],
            'id': 'frogger',
            'title': 'Frogger'},
           {'desc': 'Side-scrolling endless runner with cyberpunk aesthetic. Jump over obstacles, duck '
                    'under beams, avoid gaps. Speed increases over time. Glitch effects throughout.',
            'hw': [],
            'id': 'glitch_runner',
            'title': 'Glitch Runner (Cyberpunk Platformer)'},
           {'desc': 'Generates random mazes using recursive backtracking (DFS). Navigate from top-left to '
                    'bottom-right exit. Timer tracks completion speed. KEY2 reveals solution path.',
            'hw': [],
            'id': 'labyrinth',
            'title': 'Random Labyrinth'},
           {'desc': 'Pingus - Free Lemmings clone on the CardputerZero LCD. Runs on Xvfb, captures via '
                    'ffmpeg x11grab to framebuffer.',
            'hw': [],
            'id': 'lemmings',
            'title': 'Lemmings (Pingus)'},
           {'desc': 'Tilt the CardputerZero to roll a marble through a maze. Uses the LSM6DS3TR '
                    'accelerometer for tilt control.',
            'hw': ['IMU'],
            'id': 'marble_maze',
            'title': 'Marble Maze (IMU)'},
           {'desc': '8x8 grid, 10 mines, 14x14px cells on a LCD.',
            'hw': [],
            'id': 'minesweeper',
            'title': 'Minesweeper'},
           {'desc': 'Intense Tron-style light cycle arena. Fast, lethal, neon. Player 1 (green) vs AI '
                    '(red). Touch anything = instant death.',
            'hw': [],
            'id': 'neon_bikes',
            'title': 'NEON BIKES (Tron Light Cycles)'},
           {'desc': "Classic vertical shoot'em up on CardputerZero LCD. Runs on Xvfb, captures via ffmpeg "
                    'x11grab to framebuffer.',
            'hw': [],
            'id': 'opentyrian',
            'title': 'OpenTyrian'},
           {'desc': 'Classic arcade maze chaser. Collect pellets and power-ups while dodging ghosts across '
                    'the board.',
            'hw': [],
            'id': 'pacman',
            'title': 'Pac-Man'},
           {'desc': 'Classic Pong on the LCD. Green-on-black theme.',
            'hw': [],
            'id': 'pong',
            'title': 'Pong Game'},
           {'desc': 'Classic 4x4 sliding tile puzzle. Arrange tiles 1-15 in order.',
            'hw': [],
            'id': 'puzzle',
            'title': 'Sliding Puzzle (15-puzzle)'},
           {'desc': 'Color and pattern memory game. Watch the flashing sequence and repeat it back as each '
                    'round gets longer.',
            'hw': [],
            'id': 'simon',
            'title': 'Simon Says'},
           {'desc': 'Based on the original Show Buttons example, this script turns your Waveshare '
                    '1.44-inch LCD HAT into a classic Snake game! 1.',
            'hw': [],
            'id': 'snake',
            'title': 'Snake Game'},
           {'desc': 'Classic Sokoban puzzle game on LCD. Push boxes onto targets. 10 built-in levels of '
                    'increasing difficulty.',
            'hw': [],
            'id': 'sokoban',
            'title': 'Sokoban'},
           {'desc': 'Classic Space Invaders on LCD with green/black theme. 4 rows of 6 aliens, 3 '
                    'destructible shields, 3 lives, score counter.',
            'hw': [],
            'id': 'space_invaders',
            'title': 'Space Invaders'},
           {'desc': 'Classic falling-block puzzle game. Move left/right, rotate with F1, soft-drop with '
                    'Down, and hard-drop with Up.',
            'hw': [],
            'id': 'tetris',
            'title': 'Tetris'},
           {'desc': 'Cyberpunk text adventure powered by Anthropic Claude API. Displays scene text with '
                    'two choices. Falls back to built-in offline adventure when no API key is available.',
            'hw': [],
            'id': 'text_adventure',
            'title': 'LLM Text Adventure'},
           {'desc': 'Play classic 3x3 Tic-Tac-Toe against an unbeatable minimax alpha-beta AI opponent.',
            'hw': [],
            'id': 'tictactoe',
            'title': 'Tic-Tac-Toe vs AI'},
           {'desc': 'Cyber warfare strategy game. Hack enemy servers, defend your network. Each side has 5 '
                    'nodes. Destroy all enemy nodes to win.',
            'hw': [],
            'id': 'war',
            'title': 'Network War'}],
 'hardware': [{'desc': 'Real-time battery fuel gauge monitor for the CardputerZero. Reads from the kernel '
                       'power_supply sysfs interface (bq27500 driver).',
               'hw': [],
               'id': 'battery_monitor',
               'title': 'Battery Monitor'},
              {'desc': 'Professional GNSS tracker with 5 views on 1.44" LCD: 1. Skyplot — polar view of '
                       'all visible satellites 2. Signals — signal strength bars sorted by SNR 3. Position '
                       '— coordinates, altitude, speed, heading 4.',
               'hw': ['GPS'],
               'id': 'gnss_skyplot',
               'title': 'GNSS Satellite Tracker Pro'},
              {'desc': 'Monitors spare GPIO pins for state changes from contact switches, PIR sensors, or '
                       'other triggers. Setup / Prerequisites: Wire sensors to GPIO pins (PIR, door '
                       'contacts, etc.). Optional: Discord webhook in config for remote alerts.',
               'hw': [],
               'id': 'gpio_tripwire',
               'title': 'GPIO Tripwire'},
              {'desc': 'Configure u-blox GPS modules directly from the device. Supports constellation '
                       'selection, navigation mode, update rate, SBAS configuration, and GPS reset.',
               'hw': ['GPS'],
               'id': 'gps_config',
               'title': 'GPS Configuration'},
              {'desc': 'Real-time GPS data display: position, satellites, altitude, speed, fix status, and '
                       'raw NMEA sentences scrolling.',
               'hw': ['GPS'],
               'id': 'gps_live',
               'title': 'GPS Live'},
              {'desc': 'GPS tracking and logging via serial GPS module. Parses NMEA sentences ($GPGGA and '
                       '$GPRMC) for position, speed, altitude, and satellite info. Logs to CSV and can '
                       'export GPX.',
               'hw': ['GPS'],
               'id': 'gps_tracker',
               'title': 'GPS Tracker'},
              {'desc': 'Probes all 127 I2C addresses on bus 1 using smbus2. Setup / Prerequisites: '
                       'Requires I2C enabled (dtparam=i2c_arm=on in config.txt). Scans /dev/i2c-1.',
               'hw': [],
               'id': 'i2c_scanner',
               'title': 'I2C Bus Scanner'},
              {'desc': 'Real-time 6-axis IMU visualisation for the LSM6DS3TR accelerometer + gyroscope. '
                       'Four selectable views: 3-D rotating cube, oscilloscope waveforms, spirit-level '
                       'bubble, and raw numeric readout.',
               'hw': ['IMU'],
               'id': 'imu_monitor',
               'title': 'IMU Monitor (LSM6DS3TR)'},
              {'desc': 'Pi LED controller for operational feedback. Controls the ACT (activity) and PWR '
                       '(power) LEDs via sysfs with predefined blink patterns and a custom timing editor.',
               'hw': [],
               'id': 'led_control',
               'title': 'LED Controller'},
              {'desc': 'Detects and configures LTE/4G modems via ModemManager (mmcli). Shows modem info, '
                       'signal strength, and connection status. Allows APN configuration and '
                       'connect/disconnect operations.',
               'hw': [],
               'id': 'lte_configure',
               'title': 'LTE/4G Modem Configuration'},
              {'desc': 'Starts an HTTP server that serves a page using the Geolocation API. User opens the '
                       'page on their smartphone, which sends GPS coordinates back to the RPi '
                       'periodically. Displays position on the LCD.',
               'hw': [],
               'id': 'mobile_gps',
               'title': 'Mobile GPS Receiver'},
              {'desc': 'Read, save and clone NFC/RFID cards. Supports PN532 (UART/I2C), ACR122U, SCL3711 '
                       'via nfcpy.',
               'hw': ['NFC Reader'],
               'id': 'nfc_reader',
               'title': 'NFC/RFID Reader & Cloner'},
              {'desc': 'Infrared transmitter utility for testing and controlling compatible Electronic '
                       'Shelf Label (ESL) displays.',
               'hw': ['IR'],
               'id': 'tagtinker_esl',
               'title': 'TagTinker ESL IR'}],
 'network': [{'desc': 'Inspect WiFi interfaces: driver, chipset, supported bands, channels, TX power, '
                      'signal level, and supported modes (managed, monitor, AP, mesh).',
              'hw': ['USB WiFi Monitor'],
              'id': 'antenna_check',
              'title': 'WiFi Antenna Diagnostics'},
             {'desc': 'CAM table overflow / ARP flooding to force a switch into hub mode. Sends massive '
                      "ARP replies with random source MACs to overflow the switch's MAC address table.",
              'hw': [],
              'id': 'arp_dos',
              'title': 'ARP DoS (CAM Overflow)'},
             {'desc': 'Dedicated ARP Man-in-the-Middle attack with IP forwarding, optional DNS '
                      'interception, and connection logging.',
              'hw': [],
              'id': 'arp_mitm',
              'title': 'ARP MITM'},
             {'desc': 'CDP/LLDP spoofing to impersonate a Cisco switch or VoIP phone. Craft CDP frames '
                      'using scapy (Dot3/LLC/SNAP + CDP payload) or raw Ethernet. Sniffs existing CDP/LLDP '
                      'to learn the environment first.',
              'hw': [],
              'id': 'cdp_spoof',
              'title': 'CDP/LLDP Spoof'},
             {'desc': 'Passive DHCP snooping for network reconnaissance. Sniffs DHCP '
                      'Discover/Offer/Request/ACK packets to map clients, hostnames, MAC addresses, lease '
                      'info, and DHCP server details.',
              'hw': [],
              'id': 'dhcp_snoop',
              'title': 'DHCP Snoop'},
             {'desc': 'DHCP starvation attack. Floods the network with DHCPDISCOVER packets using random '
                      "MAC addresses to exhaust the DHCP server's lease pool. Works on eth0 or wlan0 (no "
                      'monitor mode needed). Loot: None (attack-only payload).',
              'hw': ['USB WiFi Monitor'],
              'id': 'dhcp_starve',
              'title': 'DHCP Starvation'},
             {'desc': 'Rogue DHCPv6 server for IPv6 MITM. Listens for DHCPv6 Solicit messages (UDP 547) '
                      'and responds with Advertise + Reply, assigning IPv6 addresses with the Pi as DNS '
                      'server.',
              'hw': [],
              'id': 'dhcpv6_rogue',
              'title': 'Rogue DHCPv6'},
             {'desc': 'Active DNS hijacking during MITM. Sniffs DNS queries (UDP 53) on the active '
                      'interface, responds with spoofed IPs for configured domains, and forwards all '
                      'others to the real DNS server (transparent proxy).',
              'hw': [],
              'id': 'dns_hijack',
              'title': 'DNS Hijack'},
             {'desc': 'Switch between iptables firewall presets on the fly. Four built-in presets plus a '
                      'user-defined custom ruleset. Presets OPEN -- Accept all traffic (flush rules, '
                      'policy ACCEPT).',
              'hw': [],
              'id': 'firewall_switcher',
              'title': 'Firewall Preset Switcher'},
             {'desc': 'DNS redirect portal with MAC whitelist. Whitelisted MACs get full internet access; '
                      'all others are redirected to a configurable portal page. Uses dnsmasq + iptables '
                      'for traffic steering.',
              'hw': [],
              'id': 'goodportal',
              'title': 'Whitelist Portal (GoodPortal)'},
             {'desc': 'HSRP (Hot Standby Router Protocol) takeover. Sniffs HSRP Hello packets to learn '
                      'group, priority, and VIP, then crafts Hello with priority 255 to become the active '
                      'router.',
              'hw': [],
              'id': 'hsrp_takeover',
              'title': 'HSRP Takeover'},
             {'desc': 'ICMP Redirect attack for traffic rerouting without ARP spoofing. Sends ICMP '
                      'Redirect (Type 5) messages to target hosts telling them to route traffic through '
                      'the Pi.',
              'hw': [],
              'id': 'icmp_redirect',
              'title': 'ICMP Redirect'},
             {'desc': 'IGMP snooping for multicast group discovery. Sniffs IGMP Membership Reports to '
                      'discover active multicast groups and identify VoIP, streaming, surveillance '
                      'cameras, IPTV, etc.',
              'hw': [],
              'id': 'igmp_snoop',
              'title': 'IGMP Snoop'},
             {'desc': 'Sends rogue Router Advertisement packets on the local network to become the default '
                      'IPv6 gateway. Victims that accept the RA will route their IPv6 traffic through the '
                      'Pi.',
              'hw': ['USB WiFi Monitor'],
              'id': 'ipv6_ra_attack',
              'title': 'IPv6 Router Advertisement Spoofing'},
             {'desc': 'Passive LLDP/CDP listener for infrastructure reconnaissance. Sniffs LLDP (EtherType '
                      '0x88cc) and CDP (multicast 01:00:0c:cc:cc:cc) frames, parses device name, model, '
                      'firmware, management IP, VLAN info, port description, and.',
              'hw': [],
              'id': 'lldp_recon',
              'title': 'LLDP/CDP Recon'},
             {'desc': 'Active LLMNR and NBT-NS query injector. Sends multicast LLMNR queries (UDP '
                      '224.0.0.252:5355) and NBT-NS broadcast queries (UDP port 137) for non-existent '
                      'hostnames to trigger hash captures by Responder or similar tools running on the.',
              'hw': [],
              'id': 'llmnr_query_inject',
              'title': 'LLMNR/NBT-NS Query Injector'},
             {'desc': 'Listens on 224.0.0.251:5353 for mDNS queries and responds with spoofed answers '
                      "pointing to the Pi's IP. Targets common service discovery types (_http, _ipp, "
                      '_airplay, _raop, _smb).',
              'hw': [],
              'id': 'mdns_poison',
              'title': 'mDNS/DNS-SD Poisoner'},
             {'desc': 'Bypasses 802.1X/NAC enforcement by creating a transparent bridge between two '
                      "Ethernet interfaces and cloning the authenticated device's MAC and IP addresses.",
              'hw': [],
              'id': 'nac_bypass',
              'title': '802.1X NAC Bypass via Transparent Bridge'},
             {'desc': 'NetBIOS Name Service (NBNS) spoofing. Listens for NBNS queries (UDP 137 broadcast) '
                      "and responds with the Pi's IP address, tricking Windows hosts into connecting to "
                      'us.',
              'hw': [],
              'id': 'nbns_spoof',
              'title': 'NBNS Spoof'},
             {'desc': 'Passive network health analyzer. Detects: STP root bridge conflicts ARP storms & '
                      'spoofing Rogue DHCP servers Broadcast storms Host discovery.',
              'hw': [],
              'id': 'net_doctor',
              'title': 'Network Doctor'},
             {'desc': 'Pure passive network tap on a transparent bridge interface. Creates br0 between two '
                      'Ethernet interfaces with no IP — captures all traffic and displays real-time '
                      'protocol statistics.',
              'hw': [],
              'id': 'network_tap',
              'title': 'Network Tap'},
             {'desc': 'Discovers network printers via nmap and sends text jobs via raw TCP (JetDirect port '
                      '9100). For authorized penetration testing only. Setup / Prerequisites nmap '
                      'installed. Network access to target printers.',
              'hw': [],
              'id': 'paper_pusher',
              'title': 'Network Printer Prank'},
             {'desc': 'Fast SYN port scanner using scapy (lighter than nmap). ARP-discovers hosts, then '
                      'SYN-scans the selected target.',
              'hw': [],
              'id': 'port_scanner',
              'title': 'Port Scanner'},
             {'desc': "Proxy ARP — respond to all ARP requests for a subnet with the Pi's MAC. All hosts "
                      'then send their traffic through the Pi instead of the real gateway. IP forwarding '
                      'relays traffic transparently.',
              'hw': [],
              'id': 'proxy_arp',
              'title': 'Proxy ARP'},
             {'desc': 'Rogue DHCP (WPAD) – Lab use only Starts a rogue DHCP server on a chosen interface '
                      'Sends WPAD option (DHCP option 252) pointing to a URL Displays status on LCD, stop '
                      'with KEY3.',
              'hw': [],
              'id': 'rogue_dhcp_wpad',
              'title': 'Rogue Dhcp Wpad'},
             {'desc': 'Multi-vector gateway takeover combining ARP poisoning, rogue DHCP, and IPv6 Router '
                      'Advertisement simultaneously. All three vectors run in parallel threads for maximum '
                      'coverage.',
              'hw': [],
              'id': 'rogue_gateway',
              'title': 'Rogue Gateway'},
             {'desc': 'Auto-detects 2 active interfaces (carrier=1) Creates a transparent bridge (br0) '
                      'with NO IP (stealth) Starts tcpdump on br0 (PCAP) Live protocol counters via tshark '
                      'Shows status/IPs on LCD Exit on KEY3.',
              'hw': [],
              'id': 'silent_bridge',
              'title': 'Stealth Bridge MITM'},
             {'desc': 'Listens for SSDP M-SEARCH queries on 239.255.255.250:1900 and responds with a fake '
                      'device advertisement (printer, media server, or router).',
              'hw': [],
              'id': 'ssdp_spoof',
              'title': 'SSDP/UPnP Device Spoofing'},
             {'desc': 'Sends STP BPDU frames with priority 0 to claim root bridge status on the local '
                      'network segment. Supports both classic STP (802.1D) and RSTP (802.1w) modes. Sniffs '
                      'existing BPDUs to display current root bridge info.',
              'hw': [],
              'id': 'stp_root_claim',
              'title': 'STP Root Bridge Takeover'},
             {'desc': 'SYN flood for testing service resilience. Sends TCP SYN packets with randomized '
                      'source IPs and ports to a user-selected host:port target.',
              'hw': [],
              'id': 'syn_flood',
              'title': 'SYN Flood'},
             {'desc': 'Sniff live TCP traffic to build a connection table, then inject TCP RST packets '
                      '(both directions) to tear down selected connections.',
              'hw': [],
              'id': 'tcp_rst_inject',
              'title': 'TCP RST Injection'},
             {'desc': 'Real-time network traffic analyzer dashboard. Sniffs on the active interface, '
                      'tracks bandwidth, protocol breakdown, top connections, and DNS query log.',
              'hw': [],
              'id': 'traffic_analyzer',
              'title': 'Traffic Analyzer'},
             {'desc': '802.1Q trunk negotiation + multi-VLAN traffic dump. Sends DTP (Dynamic Trunking '
                      'Protocol) frames to negotiate a trunk with a Cisco switch, then sniffs tagged '
                      'traffic from all VLANs.',
              'hw': [],
              'id': 'trunk_dump',
              'title': 'Trunk Dump'},
             {'desc': 'VLAN hopping via double-tagging and DTP spoofing. Discovers the native VLAN by '
                      'sniffing, then sends double-tagged frames or DTP negotiation packets to reach '
                      'target VLANs.',
              'hw': [],
              'id': 'vlan_hopper',
              'title': 'VLAN Hopper'},
             {'desc': 'Inject a rogue WPAD configuration via DHCP Option 252 to route all victim HTTP '
                      'traffic through a local proxy on the Pi.',
              'hw': [],
              'id': 'wpad_proxy',
              'title': 'WPAD Proxy Injection'}],
 'nfc_rfid': [{'desc': 'MIFARE Classic read/write/clone/emulate via CardputerZero Cap HAT.',
               'hw': ['NFC Reader'],
               'id': 'cap_flipper_nfc',
               'title': 'NFC Cap HAT (ST25R3916)'},
              {'desc': 'Auto-crack MIFARE Classic keys using Chameleon Ultra hardware-accelerated crypto '
                       'attacks: batch key check, darkside, nested, hardnested. Full dump + optional load '
                       'into emulation slot.',
               'hw': ['Chameleon Ultra'],
               'id': 'cu_autopwn',
               'title': 'Chameleon Ultra Autopwn'},
              {'desc': 'Device info dashboard: battery, firmware, slots overview.',
               'hw': ['Chameleon Ultra'],
               'id': 'cu_battery',
               'title': 'Chameleon Ultra Dashboard'},
              {'desc': 'Read a card and store it into a Chameleon slot for emulation. Supports MIFARE '
                       'Classic, Ultralight/NTAG, and EM4100.',
               'hw': ['Chameleon Ultra'],
               'id': 'cu_clone',
               'title': 'Chameleon Ultra Quick Clone'},
              {'desc': 'Read 125kHz badges (EM4100, HID Prox) using Chameleon Ultra.',
               'hw': ['Chameleon Ultra'],
               'id': 'cu_lf_reader',
               'title': 'Chameleon Ultra LF Reader'},
              {'desc': 'Manage the 8 emulation slots on the Chameleon Ultra.',
               'hw': ['Chameleon Ultra'],
               'id': 'cu_slot_manager',
               'title': 'Chameleon Ultra Slot Manager'},
              {'desc': 'Read, identify and clone Nintendo Amiibo (NTAG215).',
               'hw': [],
               'id': 'nfc_amiibo',
               'title': 'Amiibo Reader/Cloner'},
              {'desc': 'Advanced brute-force beyond dictionary: UID-derived keys, patterns, incremental.',
               'hw': [],
               'id': 'nfc_bruteforce',
               'title': 'NFC Key Brute-force'},
              {'desc': 'Clone NFC cards: load a saved dump and write it to a new card. Supports MIFARE '
                       'Classic and Ultralight/NTAG. Magic card (Gen1a/Gen2) detection for UID cloning.',
               'hw': [],
               'id': 'nfc_cloner',
               'title': 'NFC Cloner'},
              {'desc': 'Brute-force MIFARE Classic sector keys with extended dictionary (~100 keys). '
                       'Visual progress grid showing cracked/locked/active sectors.',
               'hw': [],
               'id': 'nfc_crack',
               'title': 'NFC Key Cracker'},
              {'desc': 'Read contactless bank cards (Visa, Mastercard, CB, Amex, etc.). Extracts: card '
                       'number, expiry, name, country, currency, CVM, transaction counter, PIN tries, and '
                       'transaction history.',
               'hw': [],
               'id': 'nfc_emv',
               'title': 'NFC EMV Reader'},
              {'desc': 'Erase/format NFC cards. Reset data, keys, or NDEF.',
               'hw': [],
               'id': 'nfc_format',
               'title': 'NFC Card Formatter'},
              {'desc': 'Interactive APDU terminal for exploring unknown NFC cards. Send arbitrary '
                       'commands, decode responses, scan SFI/records.',
               'hw': [],
               'id': 'nfc_fuzzer',
               'title': 'NFC APDU Fuzzer'},
              {'desc': 'Read hotel key cards (MIFARE Classic) with hospitality-specific key dictionary. '
                       'Targets: Assa Abloy/VingCard, Dormakaba, Onity, Salto, ASSA ABLOY Hospitality.',
               'hw': [],
               'id': 'nfc_hotel',
               'title': 'Hotel Card Reader'},
              {'desc': 'Detect magic card type: Gen1a, Gen2 (CUID), Gen3 (UFUID), Gen4 (GDM), or Original.',
               'hw': [],
               'id': 'nfc_magic',
               'title': 'Magic Card Detector'},
              {'desc': 'Write NDEF records (URL, text, WiFi config) to Ultralight/NTAG tags. Useful for '
                       'demos, rickrolls, and WiFi config sharing.',
               'hw': [],
               'id': 'nfc_ndef_writer',
               'title': 'NFC NDEF Writer'},
              {'desc': 'Read and identify NFC cards. Supports MIFARE Classic, Ultralight, NTAG, EMV. '
                       'Auto-detects PN532 (UART/I2C) and USB readers (ACR122U, SCL3711).',
               'hw': ['NFC Reader'],
               'id': 'nfc_reader',
               'title': 'NFC Reader'},
              {'desc': 'Browse, view, delete and export saved NFC card dumps. Supports Flipper Zero .nfc '
                       'export format.',
               'hw': [],
               'id': 'nfc_saved',
               'title': 'NFC Card Manager'},
              {'desc': 'Continuous NFC detection. Shows card type, UID, and for EMV cards extracts the '
                       'masked PAN and app name on the fly.',
               'hw': [],
               'id': 'nfc_scanner',
               'title': 'NFC Scanner'},
              {'desc': 'Read transit cards (Calypso/Navigo, MIFARE DESFire-based transport). Extracts: '
                       'card type, environment, contracts, counters, last events.',
               'hw': [],
               'id': 'nfc_transport',
               'title': 'Transport Card Reader'},
              {'desc': 'Auto-crack MIFARE Classic keys using Proxmark3 darkside/nested/hardnested attacks. '
                       'Requires: Proxmark3 (Easy, RDV2, or RDV4).',
               'hw': ['Proxmark3'],
               'id': 'pm3_autopwn',
               'title': 'PM3 Autopwn'},
              {'desc': 'Read and dump HID iCLASS/PicoPass cards using Proxmark3. Extracts CSN, config, '
                       'credential blocks, and Wiegand data.',
               'hw': ['Proxmark3'],
               'id': 'pm3_iclass',
               'title': 'PM3 iCLASS'},
              {'desc': 'Brute-force HID/EM4100 badge IDs using Proxmark3 LF simulation. Iterates card '
                       'numbers for a given facility code, simulating each badge on the PM3 antenna so a '
                       'nearby reader will accept or reject it.',
               'hw': ['Proxmark3'],
               'id': 'pm3_lf_brute',
               'title': 'PM3 LF Brute'},
              {'desc': 'Clone 125kHz badges (EM4100, HID, Indala) to T5577 using Proxmark3. Browse saved '
                       'LF dumps and write to blank T5577 cards.',
               'hw': ['Proxmark3'],
               'id': 'pm3_lf_clone',
               'title': 'PM3 LF Clone'},
              {'desc': 'Read 125kHz badges (EM4100, HID, Indala, AWID) using Proxmark3.',
               'hw': ['Proxmark3'],
               'id': 'pm3_lf_reader',
               'title': 'PM3 LF Reader'},
              {'desc': 'Passive NFC/RFID sniffing — capture card-reader communications and extract keys. '
                       'Requires Proxmark3. Modes: HF Sniff 13.56 MHz (ISO14443A / MIFARE) LF Sniff 125 '
                       'kHz (HID / EM4100 / Indala)',
               'hw': ['Proxmark3'],
               'id': 'pm3_sniff',
               'title': 'PM3 Sniffer'}],
 'reconnaissance': [{'desc': 'Active Directory reconnaissance via LDAP. Connects to a discovered or '
                             'user-specified LDAP server on port 389/636, attempts anonymous bind then '
                             'null-session enumeration.',
                     'hw': [],
                     'id': 'ad_recon',
                     'title': 'Active Directory Reconnaissance'},
                    {'desc': 'Real-time spectrum analyzer with channel filters for targeted RF analysis. '
                             'Features dynamic display with auto-scaling, peak frequency detection, and '
                             'instant filter switching via LEFT/RIGHT buttons.',
                     'hw': ['USB WiFi Monitor'],
                     'id': 'analyzer',
                     'title': 'RF Spectrum Analyzer'},
                    {'desc': 'Puts wlan1 into monitor mode and runs airodump-ng to capture AP statistics. '
                             'Parses the CSV output every 3 seconds and presents a scrollable dashboard of '
                             'access points with details.',
                     'hw': ['USB WiFi Monitor'],
                     'id': 'ap_hackstats',
                     'title': 'AP Statistics Dashboard'},
                    {'desc': 'Slow, stealthy ARP scanner for the local subnet. Randomizes host order, adds '
                             'jittered delays (1-3 s between probes), and optionally spoofs the source '
                             'MAC. Displays a progress bar and discovered hosts on the LCD.',
                     'hw': [],
                     'id': 'arp_scan_stealth',
                     'title': 'Stealthy ARP Scanner'},
                    {'desc': 'Automated background-style Nmap scanner that periodically scans your active '
                             'subnet and saves reports into loot/Nmap/.',
                     'hw': [],
                     'id': 'autoNmapScan',
                     'title': 'Periodic Nmap Scan'},
                    {'desc': 'Plug-and-pwn: runs AUTOMATICALLY on launch with zero user interaction. Setup '
                             '/ Prerequisites: Optional: Discord webhook in '
                             '/root/Raspyjack/discord_webhook.txt for auto-exfil of results. Sequence: 1.',
                     'hw': [],
                     'id': 'auto_recon',
                     'title': 'Automated Reconnaissance'},
                    {'desc': 'Collects AD data in BloodHound-compatible JSON format. Primary: '
                             'bloodhound-python (pip). Fallback: ldapsearch + manual JSON.',
                     'hw': [],
                     'id': 'bloodhound_collector',
                     'title': 'BloodHound Collector'},
                    {'desc': 'Discovers nearby Bluetooth Classic devices using hcitool scan, then '
                             'enumerates SDP services for each device with sdptool browse. Setup / '
                             'Prerequisites: Requires Bluetooth adapter. Loot: '
                             '/root/Raspyjack/loot/BTClassic/<timestamp>.json.',
                     'hw': [],
                     'id': 'bt_scan_classic',
                     'title': 'Bluetooth Classic Scanner'},
                    {'desc': 'Filters nearby WiFi devices by security-camera OUI vendors (Ring, Blink, '
                             'Wyze, Hikvision, Nest, etc.) to spot cameras.',
                     'hw': ['USB WiFi Monitor'],
                     'id': 'cam_finder',
                     'title': 'Camera Finder'},
                    {'desc': '8-stage CCTV camera discovery pipeline (Evil-M5Project port): 1. Port scan '
                             'targets for camera ports (80, 443, 554, 8080-8099, etc.) 2. HTTP heuristics: '
                             'detect camera web interfaces 3.',
                     'hw': [],
                     'id': 'cctv_scanner',
                     'title': 'CCTV Camera Scanner'},
                    {'desc': 'Streams MJPEG video feeds to the LCD. Loads camera URLs from '
                             '/root/Raspyjack/loot/CCTV/cctv_live.txt (format: Name | URL). Also accepts '
                             'manual URL input via config file.',
                     'hw': [],
                     'id': 'cctv_viewer',
                     'title': 'CCTV Live Viewer'},
                    {'desc': 'Scans discovered hosts on HTTPS-related ports (443, 8443, 993, 995, 465, '
                             '636) and extracts certificate details: CN, SAN entries, issuer, validity '
                             'dates, key size, and self-signed status.',
                     'hw': [],
                     'id': 'cert_scanner',
                     'title': 'TLS Certificate Scanner'},
                    {'desc': 'Focuses on wireless clients detected via airodump-ng. Shows client MACs, '
                             'associated BSSIDs, probe requests, packet counts, and OUI vendor lookups. '
                             'Loot: /root/Raspyjack/loot/ClientStats/clients_YYYYMMDD_HHMMSS.json.',
                     'hw': ['USB WiFi Monitor'],
                     'id': 'client_hackstats',
                     'title': 'Client Statistics Dashboard'},
                    {'desc': 'Sends HTTP requests using curl and displays the response on the LCD. '
                             'Supports GET, POST, HEAD, and OPTIONS methods.',
                     'hw': [],
                     'id': 'curly',
                     'title': 'Interactive HTTP Probe (curly)'},
                    {'desc': 'Anti-surveillance scanner. Detects nearby WiFi + BLE devices and flags '
                             'trackers (AirTag, Tile, SmartTag) or persistent followers.',
                     'hw': ['USB WiFi Monitor'],
                     'id': 'device_scout',
                     'title': 'Device Scout'},
                    {'desc': 'Passively sniffs local DNS (UDP 53) and NetBIOS (UDP 137) broadcasts to '
                             'discover hostnames, domains, and internal services.',
                     'hw': [],
                     'id': 'dns_leaker',
                     'title': 'DNS / NBNS Network Leaker'},
                    {'desc': 'DETECTION ONLY -- no exploitation code. Scans local subnet for port 445 '
                             'hosts, then sends SMB1 negotiate + session setup + tree connect + Trans2 '
                             'PeekNamedPipe to check MS17-010.',
                     'hw': [],
                     'id': 'eternalblue_checker',
                     'title': 'EternalBlue (MS17-010) Checker'},
                    {'desc': 'Scans for available ethernet interfaces and performs host discovery via '
                             'arp-scan with optional nmap deep scanning on selected hosts. Loot: '
                             '/root/Raspyjack/loot/EthernetRecon/<timestamp>.json.',
                     'hw': [],
                     'id': 'ethernet_recon',
                     'title': 'Ethernet Interface Recon'},
                    {'desc': 'Scans for access points via airodump-ng and provides in-depth examination of '
                             'a selected AP including band detection, encryption details, beacon/data '
                             'counts, and connected client enumeration.',
                     'hw': ['USB WiFi Monitor'],
                     'id': 'examine_ap',
                     'title': 'Detailed AP Examination'},
                    {'desc': 'Monitors WiFi probe requests via tcpdump on a monitor-mode interface '
                             '(wlan1mon) and detects "flocks" -- groups of devices that consistently '
                             'appear and disappear together within a correlation window.',
                     'hw': ['USB WiFi Monitor'],
                     'id': 'flock_detect',
                     'title': 'Device Flock Detector'},
                    {'desc': 'Scans for nearby BLE devices using hcitool lescan, lets the user select one, '
                             'then connects via gatttool to enumerate all GATT services, characteristics, '
                             'and their properties (read/write/notify).',
                     'hw': [],
                     'id': 'gatt_enum',
                     'title': 'BLE GATT Enumerator'},
                    {'desc': 'Continuous BLE surveillance to detect smart glasses (Meta Ray-Ban, Snap '
                             'Spectacles, etc.) via manufacturer Company IDs and service UUIDs. '
                             'Full-screen FLASH alert on detection.',
                     'hw': [],
                     'id': 'glasses_detector',
                     'title': 'Smart Glasses Detector'},
                    {'desc': 'Runs gobuster dir against a URL. Wordlists: install_raspyjack.sh fills '
                             'loot/wordlists and loot/wordlists/dirbuster (with /usr/share fallbacks). '
                             'Authorized testing only; generates many HTTP requests. Loot: '
                             '/root/Raspyjack/loot/Gobuster/.',
                     'hw': [],
                     'id': 'gobuster_dir',
                     'title': 'Gobuster (dir mode)'},
                    {'desc': 'Lists .cap and .pcap capture files from the loot directory, then analyses '
                             'selected files for WPA handshake quality using aircrack-ng and tcpdump.',
                     'hw': [],
                     'id': 'handshake_quality',
                     'title': 'WPA Handshake Quality Checker'},
                    {'desc': 'Professional-grade multi-service honeypot with interactive shells, threat '
                             'intelligence, GeoIP enrichment, and SIEM dashboard.',
                     'hw': [],
                     'id': 'honeypot',
                     'title': 'Honeypot SIEM Pro'},
                    {'desc': 'Professional-grade multi-service honeypot with interactive shells, threat '
                             'intelligence, GeoIP enrichment, and SIEM dashboard.',
                     'hw': [],
                     'id': 'honeypot_siem',
                     'title': 'Honeypot SIEM Pro'},
                    {'desc': 'Monitors network traffic passively via tcpdump and displays a live dashboard '
                             'with packet counts, unique IPs, top talkers, and protocol distribution on '
                             'the LCD. Loot: /root/Raspyjack/loot/Sentinel/<timestamp>.json.',
                     'hw': [],
                     'id': 'jelly_sentinel',
                     'title': 'Passive Traffic Sentinel'},
                    {'desc': 'Multi-protocol wireless scanner using Kismet as backend. LCD shows detected '
                             'devices (WiFi, BT, BLE, etc.) with live stats. Full WebUI available via '
                             'EmberJack WebUI → Kismet.',
                     'hw': [],
                     'id': 'kismet_scanner',
                     'title': 'Kismet Scanner'},
                    {'desc': 'Comprehensive LDAP dump of Active Directory environments. Auto-detects '
                             'domain controllers (ports 389/636), attempts anonymous bind first, then '
                             'authenticated bind using credentials from config.',
                     'hw': [],
                     'id': 'ldap_dumper',
                     'title': 'LDAP Dumper'},
                    {'desc': 'Detects nearby WiFi security cameras by vendor OUI and monitors their signal '
                             'strength in real time.',
                     'hw': ['USB WiFi Monitor'],
                     'id': 'livecam_detector',
                     'title': 'LiveCam Detector'},
                    {'desc': 'Resolves MAC addresses to vendor names using an OUI database. Downloads the '
                             'IEEE OUI list if available, otherwise falls back to a built-in dict of ~200 '
                             'common vendors.',
                     'hw': [],
                     'id': 'mac_lookup',
                     'title': 'MAC Address OUI Vendor Lookup'},
                    {'desc': 'Discovers devices on the local network via mDNS (multicast DNS). Listens for '
                             'mDNS announcements and actively queries common service types to enumerate '
                             'hostnames, IPs, MACs, and services.',
                     'hw': [],
                     'id': 'mdns_scanner',
                     'title': 'mDNS / Bonjour Scanner'},
                    {'desc': 'This payload prompts for a username, runs Navarro, saves JSON results under '
                             'loot/OSINT/, and shows found profile URLs on the 128×128 LCD.',
                     'hw': [],
                     'id': 'navarro',
                     'title': 'Navarro username checker'},
                    {'desc': 'Combines ARP scan results and Nmap loot to render a simple network topology '
                             'on the LCD. Gateway at top, hosts arranged by subnet. Nodes are small '
                             'labelled boxes with lines to the gateway.',
                     'hw': [],
                     'id': 'network_mapper',
                     'title': 'Visual Network Topology Mapper'},
                    {'desc': 'Multi-tab reconnaissance dashboard combining WiFi AP scanning, BLE device '
                             'scanning, and network connection monitoring into a unified interface on the '
                             'LCD. Loot: /root/Raspyjack/loot/NorseRecon/<timestamp>.json.',
                     'hw': ['USB WiFi Monitor'],
                     'id': 'norse_recon',
                     'title': 'Norse Recon Suite'},
                    {'desc': 'Checks username existence across ~30 popular websites via HTTP requests. '
                             'Uses a character picker to input the target username, then probes each site '
                             'in a background thread.',
                     'hw': [],
                     'id': 'osint_username',
                     'title': 'Username OSINT Checker'},
                    {'desc': 'p0f-style passive OS detection from network traffic. Sniffs TCP SYN packets '
                             'with scapy and analyses: initial TTL, TCP window size, MSS, TCP options '
                             'order, and DF bit to infer the remote operating system.',
                     'hw': [],
                     'id': 'passive_os_detect',
                     'title': 'Passive OS Fingerprinting'},
                    {'desc': 'Streams RTSP (H.264/H.265) video feeds to the LCD using OpenCV. Loads '
                             'cameras from: /root/Raspyjack/loot/CCTV/cctv_live.txt (scanner output) '
                             '/root/Raspyjack/config/rtsp_viewer/manual_urls.txt (manual) URL format: Name '
                             '| rtsp://...',
                     'hw': [],
                     'id': 'rtsp_viewer',
                     'title': 'RTSP Live Viewer'},
                    {'desc': 'Fast service banner grabber. Reads discovered hosts from Nmap loot or '
                             'performs its own quick port scan on common ports. Connects to each open '
                             'port, sends a protocol-appropriate probe, and captures the banner response.',
                     'hw': [],
                     'id': 'service_banner',
                     'title': 'Service Banner Grabber'},
                    {'desc': 'Queries the free Shodan InternetDB API (no API key required) for IP '
                             'intelligence. Auto-detects public IP or allows manual entry via a character '
                             'picker. Displays open ports, hostnames, CVEs, CPEs, and tags.',
                     'hw': [],
                     'id': 'shodan_query',
                     'title': 'Shodan InternetDB Query'},
                    {'desc': 'Detects active subnet Scans for SMB (445/tcp) open hosts (no exploitation) '
                             'Saves results to loot/SMB/ Displays results on LCD, scrollable with UP/DOWN, '
                             'exit on KEY3.',
                     'hw': [],
                     'id': 'smb_probe',
                     'title': 'SMB Probe (port 445)'},
                    {'desc': 'Discovers hosts with SMB (port 445) open on the local network, enumerates '
                             'accessible shares via null/guest sessions, crawls files recursively (depth '
                             '3), and flags sensitive files (configs, credentials, keys, scripts, '
                             'backups).',
                     'hw': [],
                     'id': 'smb_share_crawler',
                     'title': 'SMB Share Crawler'},
                    {'desc': 'Scans all loot directories for captured credentials: Responder logs, HTTP '
                             'creds, cracked NTLM, cracked WPA, Telnet captures, credential sniffing, '
                             'SSDP, Captive Portal, and Enterprise Evil Twin.',
                     'hw': [],
                     'id': 'sniff_creds_live',
                     'title': 'Credential Aggregator Dashboard'},
                    {'desc': 'Scans WiFi SSIDs and probe requests for patterns associated with hidden '
                             'cameras: IPCAM, IPC-, PV-, CAMERA, CAM-*. Also checks for known camera '
                             'manufacturer OUIs in nearby device MAC addresses.',
                     'hw': ['USB WiFi Monitor'],
                     'id': 'spycam_detector',
                     'title': 'Hidden Camera / Spy-cam Detector'},
                    {'desc': 'Complete subnet mapping: ARP scan to discover hosts, then quick SYN scan of '
                             'the top 20 ports on each discovered host, plus OS hint derived from ICMP TTL '
                             'values.',
                     'hw': [],
                     'id': 'subnet_mapper',
                     'title': 'Subnet Mapper'},
                    {'desc': 'Continuously monitors the local network for a specific MAC address. Uses ip '
                             'neigh show and optionally arp-scan -l to detect when the target device '
                             'appears. Flashes the screen and logs every detection with timestamp and IP '
                             'address.',
                     'hw': [],
                     'id': 'trig_mac',
                     'title': 'MAC Address Trigger'},
                    {'desc': 'Discovers all accessible VLANs on the network using passive and active '
                             'techniques: CDP/LLDP/DTP sniffing, 802.1Q VLAN hopping via tagged '
                             'subinterfaces, DHCP lease probing, and optional SNMP enumeration.',
                     'hw': [],
                     'id': 'vlan_map',
                     'title': 'VLAN Map'},
                    {'desc': 'Native BLE scanner that detects nearby Flipper Zero devices and suspicious '
                             'BLE advertisement spam with live threat summaries.',
                     'hw': [],
                     'id': 'wall_of_flippers',
                     'title': 'Wall of Flippers'},
                    {'desc': 'Professional wardriving scanner with multi-card support, GPS tracking, and '
                             'Wigle-compatible export.',
                     'hw': ['GPS'],
                     'id': 'wardriving',
                     'title': 'Wardriving'},
                    {'desc': 'Replay wardriving sessions on the LCD screen. Shows AP positions on a '
                             'mini-map with OSM tile background. Views: MAP All APs at once, colored by '
                             'security REPLAY Chronological playback with moving cursor STATS Session '
                             'summary.',
                     'hw': [],
                     'id': 'wardriving_replay',
                     'title': 'Wardriving Replay'},
                    {'desc': 'Performs WHOIS lookups and reverse DNS resolution for external IPs. Can load '
                             'unique IPs from loot/MITM/ pcap summaries or accept manual cycling through '
                             'discovered IPs.',
                     'hw': [],
                     'id': 'whois_lookup',
                     'title': 'WHOIS + Reverse DNS Lookup'},
                    {'desc': 'Passive 802.11 sniffer on a USB WiFi dongle in monitor mode. Setup / '
                             'Prerequisites: Requires USB WiFi dongle capable of monitor mode.',
                     'hw': ['USB WiFi Monitor'],
                     'id': 'wifi_client_map',
                     'title': 'WiFi Client-AP Association Mapper'}],
 'remote_access': [{'desc': 'Bi-directional C2 via Discord bot using HTTP API (urllib only). Polls a '
                            'Discord channel for commands, executes them, sends results back.',
                    'hw': [],
                    'id': 'pagecord',
                    'title': 'Pagecord (Discord C2 Bot)'},
                   {'desc': 'Post-exploitation persistence installer for authorized penetration testing. '
                            'Uses impacket/smbclient to plant persistence on compromised Windows targets. '
                            'Methods: 1. Scheduled Task (schtasks via psexec) 2.',
                    'hw': [],
                    'id': 'persistence_planter',
                    'title': 'Persistence Planter'},
                   {'desc': 'Starts a SOCKS5 proxy server for network pivoting. Supports the CONNECT '
                            'command (TCP tunneling) so remote tools can route through the Pi into the '
                            'internal network.',
                    'hw': [],
                    'id': 'pivot_proxy',
                    'title': 'SOCKS5 Pivot Proxy'},
                   {'desc': 'Forward a local port to a remote host:port. Supports multiple simultaneous '
                            'forwarding rules with bidirectional TCP relay. Loot: none (service tool, no '
                            'data to loot)',
                    'hw': [],
                    'id': 'port_forwarder',
                    'title': 'TCP Port Forwarder'},
                   {'desc': 'Generate reverse shell one-liners, serve them via HTTP, start listeners. '
                            'Loots generated commands to /root/Raspyjack/loot/Shells/.',
                    'hw': [],
                    'id': 'reverse_shell_gen',
                    'title': 'Reverse Shell Generator & Server'},
                   {'desc': 'Establishes a persistent reverse SSH tunnel using autossh with automatic '
                            'reconnection. Config stored in JSON.',
                    'hw': [],
                    'id': 'reverse_ssh',
                    'title': 'Persistent Reverse SSH Tunnel'},
                   {'desc': 'Interactive PTY-backed bash shell rendered directly on the screen with full '
                            'keyboard typing, history, and quick commands.',
                    'hw': [],
                    'id': 'shell',
                    'title': 'Linux Shell'},
                   {'desc': 'Reverse shell over HTTPS with keepalive and exponential backoff. Polls a '
                            'server for commands via GET, sends output via POST.',
                    'hw': [],
                    'id': 'stealthlink',
                    'title': 'StealthLink'},
                   {'desc': 'Start, stop, check IP/exit-node status, and manage your Tailscale mesh VPN '
                            'connection directly from EmberJack.',
                    'hw': [],
                    'id': 'tailscale_control',
                    'title': 'Tailscale Control'}],
 'sdr': [{'desc': 'CC1101 Cap HAT Sub-GHz interface cloned from Flipper Zero / Momentum. Read, Read RAW, '
                  'Saved, Frequency Analyzer. Decodes 80+ protocols: Princeton, CAME, Nice FLO, KeeLoq, '
                  'weather, etc. Flipper .sub file format for save/load/replay.',
          'hw': ['CC1101'],
          'id': 'cap_flipper_subghz',
          'title': 'Sub-GHz (Flipper Zero-style)'},
         {'desc': 'Walkie-talkie-style text chat over CC1101 at 868 MHz (2-FSK). Messages are '
                  'sent/received as packets with sync word + CRC. Uses the TCA8418 keyboard for typing on '
                  'CardputerZero.',
          'hw': ['CC1101'],
          'id': 'cc1101_chat',
          'title': 'CC1101 Sub-GHz Chat'},
         {'desc': 'Real-time spectrum analyzer and waterfall display using CC1101 RSSI sweep. Covers '
                  '300-928 MHz sub-GHz bands. No RTL-SDR needed.',
          'hw': ['RTL-SDR', 'CC1101'],
          'id': 'cc1101_spectrum',
          'title': 'CC1101 Sub-GHz Spectrum Analyzer'},
         {'desc': 'Track aircraft via ADS-B (1090 MHz) using RTL-SDR. Decodes Mode-S messages: callsign, '
                  'position, altitude, speed. Enriches with offline aircraft database (registration, type, '
                  'operator).',
          'hw': ['RTL-SDR'],
          'id': 'sdr_adsb',
          'title': 'ADS-B Aircraft Tracker'},
         {'desc': 'Receive and decode APRS packets on 144.800 MHz using RTL-SDR + Direwolf. Displays '
                  'stations on LCD map with distance/bearing from observer.',
          'hw': ['RTL-SDR'],
          'id': 'sdr_aprs',
          'title': 'APRS Station Tracker'},
         {'desc': 'Receive weather satellite images from space using RTL-SDR + SatDump. Predicts Meteor '
                  'M2-3/M2-4 passes, captures LRPT QPSK signal, decodes images with SatDump.',
          'hw': ['RTL-SDR'],
          'id': 'sdr_meteor',
          'title': 'Meteor M2 Satellite Receiver'},
         {'desc': 'Passive pager message decoder for POCSAG and FLEX protocols. Receives pager '
                  'transmissions on common frequencies and decodes messages in real-time using rtl_fm + '
                  'multimon-ng. Completely legal passive radio reception.',
          'hw': ['RTL-SDR'],
          'id': 'sdr_pocsag',
          'title': 'POCSAG/FLEX Pager Decoder'},
         {'desc': 'Flipper Zero-style capture and replay for ISM bands (315/433/868/915 MHz). Record raw '
                  'IQ signals, browse a capture library, and replay via rpitx.',
          'hw': ['RTL-SDR'],
          'id': 'sdr_replay',
          'title': 'SDR Capture & Replay'},
         {'desc': 'Multi-band radio scanner with squelch-based signal detection. Listens to Airband (AM), '
                  'Marine VHF, PMR446, FM Broadcast, Emergency. Scans frequencies and pauses on active '
                  'signals.',
          'hw': ['RTL-SDR'],
          'id': 'sdr_scanner',
          'title': 'Radio Scanner'},
         {'desc': 'Flipper Zero-style ISM band analyzer for 433/868 MHz. Decodes 200+ protocols: remotes '
                  '(CAME, NICE, etc.), weather stations, doorbells, car keys, home automation sensors, '
                  'tire pressure monitors.',
          'hw': ['RTL-SDR'],
          'id': 'sdr_subghz',
          'title': 'Sub-GHz Analyzer'},
         {'desc': 'Full-featured SDR radio suite with waterfall display, FM radio, frequency scanner, band '
                  'presets, settings, and IQ recording. Supports RTL-SDR, HackRF, and any '
                  'SoapySDR-compatible device.',
          'hw': ['RTL-SDR'],
          'id': 'sdr_suite',
          'title': 'SDR Radio Suite'}],
 'usb': [{'desc': 'BadUSB detector payload Watches for USB insertion events and raises a RED alert if: a '
                  'new USB keyboard starts sending keystrokes quickly after insertion a removable '
                  'mass-storage device is mounted.',
          'hw': [],
          'id': 'badusb_detector',
          'title': 'Badusb Detector'},
         {'desc': 'Browse, preview, and execute DuckyScript payloads stored in '
                  '/root/Raspyjack/payloads/hid_scripts/. Bundles a set of common offensive scripts and '
                  'allows editing the ATTACKER_IP placeholder.',
          'hw': [],
          'id': 'ducky_library',
          'title': 'DuckyScript Library & Launcher'},
         {'desc': 'Configure the Pi Zero as a USB HID keyboard via Linux USB gadget API (configfs). Load '
                  'and execute DuckyScript-style payloads. Setup / Prerequisites: Requires Pi Zero USB OTG '
                  'port. Connect Pi to target via USB.',
          'hw': ['USB OTG'],
          'id': 'hid_injector',
          'title': 'HID Keyboard Injector (BadUSB)'},
         {'desc': 'Configure the Pi as a USB RNDIS/ECM Ethernet adapter using configfs. When plugged into '
                  'a target host, all traffic flows through the Pi enabling DNS spoofing, credential '
                  'sniffing, and response injection.',
          'hw': ['USB OTG'],
          'id': 'usb_ethernet_mitm',
          'title': 'USB Ethernet MITM'},
         {'desc': 'The Pi sits between a USB keyboard and the target computer. USB port 1 (host mode) '
                  'reads keystrokes via evdev. USB port 2 (OTG) is configured as a HID gadget (/dev/hidg0) '
                  'that forwards every keystroke transparently to the target.',
          'hw': ['USB OTG'],
          'id': 'usb_keylogger',
          'title': 'Transparent USB HID Keylogger Proxy'},
         {'desc': 'Creates a FAT32 disk image and configures the Pi Zero as a USB mass storage device via '
                  'Linux configfs. Can pre-load files from template directories. Monitors host access via '
                  'inotify on the mount point.',
          'hw': ['USB OTG'],
          'id': 'usb_mass_storage',
          'title': 'USB Mass Storage Gadget'}],
 'utilities': [{'desc': 'Checks, installs and configures all GPS dependencies for gps_tracker and '
                        'wardriving payloads. Steps: 1. pyserial (gps_tracker) 2. scapy (wardriving) 3. '
                        'gpsd daemon (apt) (wardriving) 4. gpsd-py3 (pip) (wardriving) 5.',
                'hw': ['GPS'],
                'id': 'GPS_Doctor',
                'title': 'GPS Setup & Doctor'},
               {'desc': 'Measures LAN throughput using iperf3 against a chosen server. Enforces Ethernet '
                        'by requiring the default route device to be an Ethernet interface.',
                'hw': [],
                'id': 'LanTest',
                'title': 'LAN Speed Test (Ethernet-only)'},
               {'desc': 'Measures internet (WAN) download/upload using Speedtest. Prefers the Python '
                        'speedtest module; falls back to the Ookla CLI (JSON). Works over whichever '
                        'interface has the default route (Ethernet or Wi-Fi).',
                'hw': [],
                'id': 'WanTest',
                'title': 'WAN Speed Test'},
               {'desc': 'Three-page WiGLE stats payload showing user, group, and upload info.',
                'hw': [],
                'id': 'WigleStats',
                'title': 'WiGLE Stats'},
               {'desc': 'Displays current time with configurable alarm. Flashes the screen when alarm '
                        'triggers until dismissed.',
                'hw': [],
                'id': 'alarm_clock',
                'title': 'Alarm Clock'},
               {'desc': 'Discover, favorite, and launch standalone Linux GUI apps (.desktop, Flatpak, Snap, '
                        'AppImage, or custom commands) fullscreen via cage/xinit with built-in uinput '
                        'keyboard mouse (Ctrl+Alt+M) and universal force-exit (Ctrl+Alt+Q).',
                'hw': [],
                'id': 'app_launcher',
                'title': 'GUI App Launcher'},
               {'desc': 'Full Obsidian Ember audio mixer and device manager: switch outputs (HDMI, '
                        '3.5mm, USB, Bluetooth), adjust per-device and microphone volumes, mix active '
                        'per-app playback streams, switch Bluetooth A2DP/Headset profiles, run L/R '
                        'channel tests, and restart PipeWire/WirePlumber.',
                'hw': [],
                'id': 'audio_manager',
                'title': 'Audio & Volume Manager'},
               {'desc': 'Checks for updates from GitHub, shows changelog, backs up configs, pulls latest '
                        'code, restores user configs, and reboots.',
                'hw': [],
                'id': 'auto_update',
                'title': 'Auto-Update (LCD-friendly)'},
               {'desc': 'Scan, pair, trust, and connect Bluetooth headphones, speakers, and HID devices, '
                        'switch audio outputs, adjust volume, and play a test chime.',
                'hw': [],
                'id': 'bluetooth_manager',
                'title': 'Bluetooth & Audio Manager'},
               {'desc': 'Adjust display backlight brightness on supported LCD panels.',
                'hw': [],
                'id': 'brightness',
                'title': 'Brightness Control'},
               {'desc': 'Interactive joystick + LCD helper to scan, pair, trust and connect a Bluetooth '
                        'keyboard (or any HID) without touching the shell.',
                'hw': [],
                'id': 'bt_keyboard_picker',
                'title': 'Bluetooth Keyboard Picker'},
               {'desc': 'Assign custom shell commands or quick actions to simultaneous button '
                        'combinations.',
                'hw': [],
                'id': 'button_combo',
                'title': 'Button Combo Actions'},
               {'desc': 'Live overview of running payloads, captured loot statistics, network interfaces, '
                        'and service health.',
                'hw': [],
                'id': 'c2_dashboard',
                'title': 'C2 Dashboard'},
               {'desc': 'Full-featured calculator with dark modern theme inspired by iOS/macOS. Supports '
                        'standard arithmetic, scientific functions, parentheses, hex/dec/bin/oct '
                        'conversion, and calculation history.',
                'hw': [],
                'id': 'calculator',
                'title': 'Scientific Calculator'},
               {'desc': 'Photo and video capture with IMX219 camera. Flow: Menu -> '
                        'Photo/Video/Gallery/Settings Photo: live preview + OK to snap (instant from '
                        'preview frame) KEY2 for hi-res capture (brief preview pause) Video: OK to start '
                        'rec (preview stops).',
                'hw': ['Camera'],
                'id': 'camera',
                'title': 'Camera'},
               {'desc': 'Checks and configures the IMX219 camera for CardputerZero. Fixes config.txt if '
                        'needed, installs dependencies, tests camera.',
                'hw': ['Camera'],
                'id': 'camera_setup',
                'title': 'Camera Setup'},
               {'desc': 'Quick diagnostics for all WiFi and Bluetooth adapters: Driver + chipset detection '
                        'Monitor mode test (real test, not just iw phy info) Frame injection test (send '
                        'deauth and verify) Supported bands (2.4 / 5 GHz) BLE scan capability test.',
                'hw': ['USB WiFi Monitor'],
                'id': 'card_tester',
                'title': 'Card Tester'},
               {'desc': 'Adjust the Cardputer frame generation settings used by raspyjack.py. Settings are '
                        'persisted as a systemd override for raspyjack.service and take effect after the '
                        'service is restarted.',
                'hw': [],
                'id': 'cardputer_tuner',
                'title': 'Cardputer Tuner'},
               {'desc': 'MAC whitelist/blacklist manager for portal access. Shows connected clients from '
                        'arp -a, allows whitelisting or blacklisting via iptables.',
                'hw': [],
                'id': 'client_access_mgr',
                'title': 'Client Access Manager'},
               {'desc': 'Browse the filesystem and display images on the LCD. Supports PNG, JPG, GIF, and '
                        'BMP files with fit/fill/stretch resize modes.',
                'hw': [],
                'id': 'display_image',
                'title': 'LCD Image Viewer'},
               {'desc': 'Switch between supported LCD screens from the device menu. Changes take effect '
                        'after service restart.',
                'hw': [],
                'id': 'display_selector',
                'title': 'Display Type Selector'},
               {'desc': 'Operation timer with phase tracking for penetration testing engagements. Set '
                        'duration, start countdown, cycle through engagement phases, and get alerts when '
                        'time expires. Optional Discord webhook notification.',
                'hw': [],
                'id': 'engagement_timer',
                'title': 'Engagement Timer'},
               {'desc': 'ESP Terminator flasher - downloads and flashes firmware from espterminator.com '
                        'catalog to any ESP32/ESP8266 board.',
                'hw': [],
                'id': 'espterminator',
                'title': 'ESP Terminator'},
               {'desc': 'Quick WiFi Connect payload Auto-connect to the strongest saved WiFi network '
                        'currently in range. If no saved network is found, suggest using the full WiFi '
                        'Manager.',
                'hw': [],
                'id': 'fast_wifi_connect',
                'title': 'Fast Wifi Connect'},
               {'desc': 'FAST WiFi Switcher - Responsive wlan0/wlan1 Control Fast, responsive interface '
                        'for switching between wlan0 and wlan1. No bullshit, no delays, just quick '
                        'switching that actually works.',
                'hw': [],
                'id': 'fast_wifi_switcher',
                'title': 'Fast Wifi Switcher'},
               {'desc': 'Browse payloads by category (same arborescence as the Payload menu), toggle '
                        'favorites, and launch them. Favorites appear in the main menu.',
                'hw': [],
                'id': 'favorites_manager',
                'title': 'Payload Favorites Manager'},
               {'desc': 'Diagnostic tool for GPS modules. Scans all serial ports (GPIO UART, USB) at '
                        'multiple baud rates looking for NMEA data. Shows raw sentences even without fix.',
                'hw': ['GPS'],
                'id': 'gps_check',
                'title': 'GPS Check'},
               {'desc': 'This built-in searchable reference manual covering every EmberJack menu, hotkey, '
                        'payload, and Raspberry Pi troubleshooting step.',
                'hw': [],
                'id': 'guide',
                'title': 'Interactive User Guide'},
               {'desc': 'Monitors loot directory for new files and sends webhook notifications (Slack, '
                        'Discord, or generic JSON) when new loot is detected.',
                'hw': [],
                'id': 'gv_alerts',
                'title': 'Webhook Notification Alerts'},
               {'desc': 'Aggregates statistics from /root/Raspyjack/loot/ subdirectories. Shows total '
                        'handshakes, credentials, scans, hosts, file counts by extension, and a 24-hour '
                        'activity timeline bar chart.',
                'hw': [],
                'id': 'hackstats_dashboard',
                'title': 'Attack Statistics Dashboard'},
               {'desc': 'Strip irrelevant frames from capture files, keeping only EAPOL handshake packets '
                        'and beacon frames needed for cracking. Uses tshark to filter: eapol || '
                        'wlan.fc.type_subtype == 0x08 Output: /root/Raspyjack/loot/Handshakes_Clean/.',
                'hw': [],
                'id': 'handshake_sanitiser',
                'title': 'Handshake File Sanitizer'},
               {'desc': 'Upload .hc22000 / .hccapx hash files to a Hashtopolis server via its REST API.',
                'hw': [],
                'id': 'hashtopolis_upload',
                'title': 'Hashtopolis Uploader'},
               {'desc': 'Convert .cap capture files to .hc22000 format for Hashcat cracking. Uses '
                        'hcxpcapngtool (preferred) or falls back to cap2hccapx. Output: '
                        '/root/Raspyjack/loot/Hashcat/.',
                'hw': [],
                'id': 'hccapx_converter',
                'title': 'HCCAPX Converter for Hashcat'},
               {'desc': 'Mirrors the LCD framebuffer to HDMI output in real-time. Runs as a background '
                        'process — does NOT take over the LCD. Launch to start, launch again to stop. '
                        'Requires: HDMI cable connected.',
                'hw': [],
                'id': 'hdmi_mirror',
                'title': 'HDMI Mirror'},
               {'desc': 'Full hardware diagnostic for CardputerZero. Tests every component: LCD, keyboard, '
                        'speaker, mic, camera, GPS, WiFi, BT, USB, GPIO, I2C, IR, battery.',
                'hw': [],
                'id': 'hw_test',
                'title': 'Hardware Test'},
               {'desc': 'Monitors incoming ICMP echo requests via tcpdump and alerts on the LCD when a '
                        'ping is received. Shows source IP, timestamp, and a running counter of total '
                        'pings.',
                'hw': [],
                'id': 'icmp_alert',
                'title': 'ICMP Ping Alert Monitor'},
               {'desc': 'Centralized network interface management: rfkill, monitor mode, up/down, IP '
                        'config, driver info. One place to control all WiFi, Ethernet, and Bluetooth '
                        'interfaces. Loot: /root/Raspyjack/loot/IfaceManager/.',
                'hw': ['USB WiFi Monitor'],
                'id': 'iface_manager',
                'title': 'Interface Manager'},
               {'desc': 'Split screen: left = eth0, right = eth1 Live status: operstate, IP, RX/TX bytes '
                        'Exit with KEY3.',
                'hw': [],
                'id': 'interface_status',
                'title': 'Interface Status (eth0/eth1)'},
               {'desc': 'Connects to an IRC server and displays channel messages on the LCD. Supports '
                        'sending messages via character picker, viewing user lists, and switching between '
                        'channels.',
                'hw': [],
                'id': 'irc_chat',
                'title': 'Minimal IRC Client'},
               {'desc': 'Real-time ISS tracker with world map, orbit prediction, next visible passes, crew '
                        'info, and satellite data.',
                'hw': [],
                'id': 'iss_tracker',
                'title': 'ISS Tracker'},
               {'desc': 'Displays the name of the key pressed on a USB keyboard connected to the Raspberry '
                        'Pi. It listens to the first detected evdev keyboard device and shows the key name '
                        'centred in bright green on the Waveshare 1.44-inch LCD.',
                'hw': [],
                'id': 'keyboard_tester',
                'title': 'Show USB Keyboard Key'},
               {'desc': 'Measures TCP connect RTT (no monitor mode), estimates jitter, and shows a rolling '
                        'graph on the LCD. Designed to work over Ethernet or managed Wi-Fi without special '
                        'privileges.',
                'hw': ['USB WiFi Monitor'],
                'id': 'latency',
                'title': 'Latency/Jitter Monitor ()'},
               {'desc': 'Browse directories and preview saved logs, scan reports, captures, and files '
                        'inside the loot/ folder.',
                'hw': [],
                'id': 'loot_browser',
                'title': 'Loot Browser'},
               {'desc': 'Flash M5Stack firmwares using the official M5Burner catalog. Downloads binaries '
                        'from m5burner-cdn.m5stack.com.',
                'hw': [],
                'id': 'm5burner',
                'title': 'M5Burner'},
               {'desc': 'Pre-download OSM map tiles for wardriving & GPS tracker. Select a region and zoom '
                        'level, tiles are cached for offline use.',
                'hw': ['GPS'],
                'id': 'map_downloader',
                'title': 'Offline Map Downloader'},
               {'desc': 'Browse downloaded map tiles on the LCD. Pan, zoom, GPS center.',
                'hw': ['GPS'],
                'id': 'map_viewer',
                'title': 'Offline Map Viewer'},
               {'desc': 'Encode text to Morse code with visual dot/dash display, or decode Morse input '
                        'tapped via buttons. Uses the standard ITU Morse table.',
                'hw': [],
                'id': 'morse_code',
                'title': 'Morse Code Encoder / Decoder'},
               {'desc': 'Interactive network tools menu: ping, traceroute, mtr, nslookup, netstat, route, '
                        'arp table.',
                'hw': [],
                'id': 'net_tools',
                'title': 'Network Tools'},
               {'desc': 'Fetches and displays RSS feed articles from configurable sources. Parses RSS XML '
                        'with xml.etree.ElementTree. Shows scrollable article titles with summaries on '
                        'selection.',
                'hw': [],
                'id': 'news_reader',
                'title': 'RSS News Reader'},
               {'desc': 'Aggregates live alerts and new-loot events produced across running EmberJack '
                        'payloads.',
                'hw': [],
                'id': 'notification_center',
                'title': 'Notification Center'},
               {'desc': 'Install apt or pip packages directly from the LCD interface. Uses a character '
                        'picker for package name input, shows install output scrollable on screen.',
                'hw': [],
                'id': 'package_installer',
                'title': 'Package Installer'},
               {'desc': 'Lists .pcap files from the loot directory and replays them using scapy. Uses '
                        'PcapReader for streaming to avoid memory issues on 512MB Pi Zero. Supports '
                        'original timing, accelerated replay, and optional MAC rewrite.',
                'hw': [],
                'id': 'packet_replay',
                'title': 'PCAP Packet Replayer'},
               {'desc': 'Generate cryptographically secure passwords with configurable length and '
                        'character sets. Passwords can be saved to the loot directory.',
                'hw': [],
                'id': 'password_generator',
                'title': 'Password Generator'},
               {'desc': 'Cron-like payload scheduler. Configure payload path and schedule (once at a '
                        'specific time, or repeat every N minutes). Stores the schedule in a JSON file and '
                        'launches payloads as subprocesses.',
                'hw': [],
                'id': 'payload_scheduler',
                'title': 'Payload Scheduler'},
               {'desc': 'Search installed payloads by keyword and view their docstrings. Scans the '
                        'payloads directory for .py files (excluding _ prefixed helpers).',
                'hw': [],
                'id': 'payload_search',
                'title': 'Payload Search'},
               {'desc': 'Smart incremental updater: fetches only new and modified payloads from GitHub. '
                        'Does NOT touch system files, configs, or loot. Flow: 1. git fetch (download '
                        'changes without applying) 2. Compare local vs remote payload files 3.',
                'hw': [],
                'id': 'payload_updater',
                'title': 'Payload Updater'},
               {'desc': 'Browse and analyze pcap/cap/pcapng files directly on the device. Supports both '
                        'WiFi captures and network (ethernet) captures.',
                'hw': [],
                'id': 'pcap_analyzer',
                'title': 'PCAP Analyzer'},
               {'desc': 'Periodically scans the local subnet with nmap and compares results against a '
                        'stored baseline. Alerts when new open ports are discovered.',
                'hw': [],
                'id': 'port_alert',
                'title': 'New Port Alert Monitor'},
               {'desc': 'Browse running processes sorted by memory usage and send signals to terminate or '
                        'force-kill them directly from the LCD.',
                'hw': [],
                'id': 'process_killer',
                'title': 'Process Manager / Killer'},
               {'desc': 'Generate and display QR codes on the 1.44" LCD for quick sharing of URLs, IPs, '
                        'WiFi credentials, or custom text.',
                'hw': [],
                'id': 'qr_generator',
                'title': 'QR Code Generator (LCD)'},
               {'desc': 'Scans QR codes and barcodes using the IMX219 camera. Continuous scanning with '
                        'live preview.',
                'hw': ['Camera'],
                'id': 'qr_scanner',
                'title': 'QR/Barcode Scanner'},
               {'desc': 'Launch and control the vendored Ragnar autonomous network reconnaissance and '
                        'service-auditing dashboard.',
                'hw': [],
                'id': 'ragnar',
                'title': 'Ragnar Network Scanner'},
               {'desc': 'Multi-port serial terminal with auto-baud detection. Supports GPIO UART and USB '
                        'serial simultaneously. Type with TCA8418 keyboard.',
                'hw': [],
                'id': 'serial_terminal',
                'title': 'Serial Terminal (Multi-port)'},
               {'desc': 'Scans for Bluetooth Low Energy devices that match known skimmer module names '
                        '(HC-05, HC-06, JDY-31, etc.). Flags suspicious devices with a red alert and logs '
                        'results to loot directory.',
                'hw': [],
                'id': 'skimmer_scanner',
                'title': 'BLE Skimmer Scanner'},
               {'desc': 'Two modes: Connected Clients (station dump) and Open APs (scan). Filter by signal '
                        'strength, export results to loot directory.',
                'hw': [],
                'id': 'strip_clients',
                'title': 'Client & AP Filter'},
               {'desc': 'Motion detection with auto-capture and video recording. Monitors camera feed, '
                        'triggers on movement.',
                'hw': ['Camera'],
                'id': 'surveillance',
                'title': 'Surveillance'},
               {'desc': 'Real-time CPU load, RAM usage, Pi temperature, disk space, uptime, network '
                        'throughput, and service status.',
                'hw': [],
                'id': 'system_monitor',
                'title': 'System Monitor'},
               {'desc': 'Audio recorder using the ES8389 built-in microphone. Enables PIO_G9 (AU_EN) LOW '
                        'to activate analog mic input.',
                'hw': ['Mic'],
                'id': 'tape_recorder',
                'title': 'Tape Recorder'},
               {'desc': 'Lightweight torrent client using transmission-daemon as backend. LCD shows '
                        'torrent list with progress, speed, seeds/peers. Full WebUI available at '
                        'http://<CZ_IP>:9091.',
                'hw': [],
                'id': 'torrent_client',
                'title': 'Torrent Client'},
               {'desc': 'Translate text between languages using the MyMemory API. Character picker for '
                        'text input, cycles through target languages. API: '
                        'https://api.mymemory.translated.net/get?q={text}&langpair={from}|{to}.',
                'hw': [],
                'id': 'translator',
                'title': 'Text Translator'},
               {'desc': 'Endlessly cycles a programmed lineup of YouTube channels/shows like a mini TV '
                        'tuner.',
                'hw': ['Internet'],
                'id': 'tv',
                'title': 'Lean-Back YouTube TV'},
               {'desc': 'Lists connected USB storage devices and provides safe unmount with filesystem '
                        'sync and power-off.',
                'hw': [],
                'id': 'usb_unmount',
                'title': 'USB Safe Unmount'},
               {'desc': 'Single-process video player: ffmpeg handles audio+video decode & sync. Video '
                        'frames → pipe → fb0 mmap. Audio → ALSA directly from ffmpeg.',
                'hw': ['Mic'],
                'id': 'video_player',
                'title': 'Video Player'},
               {'desc': 'Fetches and displays current weather conditions from wttr.in. Supports city '
                        'selection with a character picker.',
                'hw': [],
                'id': 'weather',
                'title': 'Weather Display'},
               {'desc': 'Displays WebUI URLs for all network interfaces, service status, and allows '
                        'restarting raspyjack-webui.service.',
                'hw': [],
                'id': 'webui',
                'title': 'WebUI Info & Control'},
               {'desc': 'Plug in your dongle, press KEY1, done. FLOW: 1. Scan USB dongles (lsusb + /sys) '
                        '2. Identify VID:PID against built-in driver database 3. Automatic install (apt / '
                        'DKMS apt / GitHub build) 4. Monitor mode test 5. LCD result screen.',
                'hw': ['USB WiFi Monitor'],
                'id': 'wifi_dongle_installer',
                'title': 'Universal WiFi Adapter Installer'},
               {'desc': 'Scan wireless networks, enter passwords, connect, save WiFi profiles, and manage '
                        'wlan0/wlan1 interfaces.',
                'hw': [],
                'id': 'wifi_manager_payload',
                'title': 'Full WiFi Manager'},
               {'desc': 'Faithful Winamp 2.x-inspired local MP3/audio player with playlist support.',
                'hw': [],
                'id': 'winamp_player',
                'title': 'Winamp Classic Player'},
               {'desc': 'Stream 720p H.264 YouTube videos and playlists with Bluetooth/PipeWire audio, '
                        'live A/V sync nudge (,/.), seeking, and MP4/MP3 downloads.',
                'hw': ['Internet'],
                'id': 'youtube',
                'title': 'YouTube Video Player'},
               {'desc': 'Full music player with continuous background audio, 16-band FFT visualizer, '
                        'synced LRC lyrics, curated Radio, Auto-Radio Mix, 6 EQ presets, playlists & '
                        'offline MP3s.',
                'hw': ['Internet'],
                'id': 'youtube_music',
                'title': 'YouTube Music Player'}],
 'wifi': [{'desc': 'Full captive portal with menu-driven management: start/stop/restart the AP, select '
                   'portal page from DNSSpoof/sites/ or built-in templates, edit SSID, manage MAC '
                   'whitelist, view captured credentials.',
           'hw': [],
           'id': 'captive_portal',
           'title': 'Captive Portal'},
          {'desc': 'SSID Injection Testing Framework for IoT & WiFi device security assessment. Broadcasts '
                   'crafted SSID payloads to detect parsing vulnerabilities, buffer overflows, and command '
                   'injection flaws in nearby devices.',
           'hw': [],
           'id': 'ciw_zeroclick',
           'title': 'CIW Zeroclick'},
          {'desc': 'Secure anonymous file sharing via WiFi captive portal. Opens a WiFi AP with a web '
                   'portal where anyone can upload and download files from a sandboxed directory. '
                   'Real-time dashboard on LCD.',
           'hw': [],
           'id': 'dead_drop',
           'title': 'WiFi Dead Drop'},
          {'desc': 'Multi-target WiFi deauthentication tool with simultaneous WPA2 handshake capture for '
                   'authorized wireless testing.',
           'hw': ['USB WiFi Monitor'],
           'id': 'deauth',
           'title': 'WiFi Deauth & Handshake'},
          {'desc': 'Captures ESP-NOW frames over the air using a WiFi adapter in monitor mode. Receives '
                   'wardriving AP data and handshake fragments from the ESP32-C5 slave.',
           'hw': ['USB WiFi Monitor', 'GPS'],
           'id': 'espnow_evil_master',
           'title': 'ESP-NOW Receiver (Monitor Mode)'},
          {'desc': 'Configurable event monitoring system. Watches for WiFi events and triggers alerts: '
                   'deauth floods, new client connections, specific MAC detection, authentication '
                   'captures, beacon floods, and probe request monitoring.',
           'hw': [],
           'id': 'event_triggers',
           'title': 'Event Triggers'},
          {'desc': 'Clone a target AP to lure clients into connecting. Serves a captive portal that '
                   'harvests credentials. Setup / Prerequisites USB WiFi dongle with monitor mode support '
                   '(e.g.',
           'hw': ['USB WiFi Monitor'],
           'id': 'evil_twin',
           'title': 'Evil Twin AP'},
          {'desc': 'Captures WPA2 4-way handshakes by scanning for APs, selecting a target, discovering '
                   'connected clients, sending targeted deauth, and capturing the EAPOL exchange. Saves as '
                   'pcap file.',
           'hw': ['USB WiFi Monitor'],
           'id': 'handshake_hunter',
           'title': 'WPA2 Handshake Hunter'},
          {'desc': 'Monitor WiFi probe requests to discover SSIDs that nearby devices are searching for, '
                   'then create a rogue AP using the most-probed SSID to lure clients. Setup / '
                   'Prerequisites USB WiFi dongle with monitor mode support (e.g.',
           'hw': ['USB WiFi Monitor'],
           'id': 'karma_ap',
           'title': 'KARMA AP'},
          {'desc': 'Captures PMKID hashes from WPA2 access points by sending association requests and '
                   'extracting the PMKID from EAPOL RSN PMKID-List. Saves in hashcat-compatible format.',
           'hw': ['USB WiFi Monitor'],
           'id': 'pmkid_grab',
           'title': 'PMKID Hash Grabber'},
          {'desc': 'Automated WiFi handshake and PMKID hunter with pixel-art face UI. Full 4-way handshake '
                   'capture (passive EAPOL sniffing) Half-handshake capture (2+ EAPOL messages, crackable '
                   'with hashcat) PMKID capture via RSN IE parsing + active.',
           'hw': [],
           'id': 'raspygotchi',
           'title': 'Pwnagotchi'},
          {'desc': 'Broadcast multiple SSIDs simultaneously using scapy beacon injection. Each SSID gets a '
                   'random but persistent BSSID. Built-in list plus custom entries from config.',
           'hw': ['USB WiFi Monitor'],
           'id': 'ssid_pool',
           'title': 'SSID Pool (Beacon Flood)'},
          {'desc': 'Security testing tool for Unitree robots (Go1, Go2, B2, H1, G1). Based on published '
                   'security research and CVE-2025-2894. WARNING: For authorized security testing and '
                   'educational purposes only.',
           'hw': [],
           'id': 'unitree_pwn',
           'title': 'Unitree Robot Security Tester'},
          {'desc': 'Monitors the WiFi airspace for target MACs and SSIDs defined in a watchlist. When a '
                   'target appears or disappears the LCD flashes red and an optional Discord webhook '
                   'notification is sent.',
           'hw': ['USB WiFi Monitor'],
           'id': 'wifi_alert',
           'title': 'WiFi Airspace Alert Monitor'},
          {'desc': 'Continuously monitors all 2.4 GHz channels for client associations. When a '
                   'Dot11AssoResp is detected, begins capturing EAPOL frames for that BSSID. If no '
                   'handshake within 10 seconds, sends a single deauth to trigger re-authentication.',
           'hw': ['USB WiFi Monitor'],
           'id': 'wifi_handshake_auto',
           'title': 'Automatic WiFi Handshake Capture'},
          {'desc': 'Passive WiFi probe request logger. Puts a USB WiFi dongle into monitor mode and sniffs '
                   'Dot11ProbeReq frames with scapy. Builds a dictionary of {MAC: set(SSIDs)} and displays '
                   'live stats on the LCD.',
           'hw': ['USB WiFi Monitor'],
           'id': 'wifi_probe_dump',
           'title': 'WiFi Probe Request Dump'},
          {'desc': 'Live WiFi reconnaissance dashboard inspired by PineAP Recon. Hops channels on a USB '
                   'monitor-mode dongle, captures beacons, probe requests and data frames, and builds a '
                   'real-time database of access points and clients.',
           'hw': ['USB WiFi Monitor'],
           'id': 'wifi_survey',
           'title': 'WiFi Recon Survey Dashboard'},
          {'desc': 'Clone a WPA-Enterprise AP and run a fake RADIUS server that accepts all '
                   'authentications while capturing EAP identities, MSCHAPv2 challenge/responses, and '
                   'EAP-GTC plaintext passwords.',
           'hw': ['USB WiFi Monitor'],
           'id': 'wpa_enterprise_evil',
           'title': 'WPA-Enterprise Evil Twin + Fake RADIUS'},
          {'desc': 'Attack WPS-enabled access points using Pixie Dust (offline) and online brute-force via '
                   'reaver/wash. Setup / Prerequisites USB WiFi dongle with monitor mode support (e.g.',
           'hw': ['USB WiFi Monitor'],
           'id': 'wps_pixie',
           'title': 'WPS Pixie Dust + Brute-Force'}]}
