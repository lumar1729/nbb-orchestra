# nbb-orchestra

Deployment tools for coordinating musical Raspberry Pi robots in the No Black Boxes course.

The main No Black Boxes repository must already be installed on each Pi at:

```text
~/NoBlackBoxes/LastBlackBox
```

## Ports

- **8000** — No Black Boxes message-board/orchestra server
- **8001** — WAV file server

## Server setup

Run the normal No Black Boxes message-board server on the laptop as usual.

Store the orchestra WAV files in one directory and serve it separately on port 8001:

```powershell
python -m http.server 8001 --directory "C:\path\to\orchestra\wav"
```

Leave this running while installing Pis or updating their WAV libraries. If Windows Firewall prompts for access, allow Python on the private/local network.

Find the server's IPv4 address with:

```powershell
ipconfig
```

You can test the WAV server from a Pi with:

```bash
curl http://SERVER_IP:8001/
```

## Initial Pi installation

First install the main No Black Boxes repository. Then, with the WAV server running, run:

```bash
curl -fsSL https://raw.githubusercontent.com/lumar1729/nbb-orchestra/main/install_orchestra.sh -o /tmp/install_orchestra.sh \
&& chmod +x /tmp/install_orchestra.sh \
&& sudo /tmp/install_orchestra.sh SERVER_IP
```

For example:

```bash
sudo /tmp/install_orchestra.sh 192.168.1.115
```

You can optionally choose the Pi's initial default WAV:

```bash
sudo /tmp/install_orchestra.sh 192.168.1.115 -d Choir.wav
```

`--default Choir` is equivalent. If no default is supplied, the first WAV alphabetically is used.

The installer:

1. Checks that `LastBlackBox` exists.
2. Installs required system packages.
3. Installs the orchestra Python code.
4. Configures Chrony against the server.
5. Downloads the WAV library from port 8001.
6. Writes the selected WAV to `default_wav.txt`.

## Installed files

```text
~/pi_message_client.py

~/NoBlackBoxes/LastBlackBox/boxes/audio/signal-processing/python/generation/
├── play_wav.py
├── assign_wavs.py
├── default_wav.txt
└── wav/
    └── *.wav
```

`play_wav.py` plays the filename stored in `default_wav.txt`.

## Starting the message client

```bash
~/NoBlackBoxes/LastBlackBox/_tmp/LBB/bin/python ~/pi_message_client.py SERVER_IP
```

For example:

```bash
~/NoBlackBoxes/LastBlackBox/_tmp/LBB/bin/python ~/pi_message_client.py 192.168.1.115
```

## Assigning orchestra parts

With the desired Pis connected, use the server's **Run command in sync** control to run:

```text
python ~/NoBlackBoxes/LastBlackBox/boxes/audio/signal-processing/python/generation/assign_wavs.py
```

Target `all`, or select the Pis that should participate.

The server creates a random speaking order. Each Pi independently assigns preference scores to the available WAVs and, in turn, proposes its favourite remaining part on the public message board. Other Pis whose current favourite is the same part can respond before the proposing Pi claims it.

The process stops when every Pi has a part or every WAV has been assigned. Assigned Pis write their result to `default_wav.txt`.

If there are more Pis than WAVs, each unassigned Pi posts:

```text
Bye chat!
```

and disconnects from the server. Restart `pi_message_client.py` when you want that Pi to reconnect.

After assignment, run `play_wav.py` in sync as usual. Each connected Pi will play its newly assigned default WAV.

## Updating orchestra code

To update `pi_message_client.py`, `play_wav.py`, and `assign_wavs.py`:

```bash
curl -fsSL https://raw.githubusercontent.com/lumar1729/nbb-orchestra/main/install_orchestra_code.sh -o /tmp/install_orchestra_code.sh \
&& chmod +x /tmp/install_orchestra_code.sh \
&& /tmp/install_orchestra_code.sh
```

Existing versions are backed up as `.backup` before replacement.

## Updating the WAV library

With the WAV server running:

```bash
curl -fsSL https://raw.githubusercontent.com/lumar1729/nbb-orchestra/main/update_wavs.sh -o /tmp/update_wavs.sh \
&& chmod +x /tmp/update_wavs.sh \
&& /tmp/update_wavs.sh SERVER_IP
```

The new library is downloaded to a temporary directory first and replaces the existing library only after all files download successfully.

To update the library and set a new default WAV at the same time:

```bash
/tmp/update_wavs.sh SERVER_IP -d Strings.wav
```

`--default Strings` is equivalent; the `.wav` extension is optional. The requested file is checked against the newly downloaded library before anything is replaced. If it is not found, the existing WAV library and default are left unchanged. If `-d`/`--default` is omitted, the existing `default_wav.txt` is left unchanged.

The default WAV-server port is 8001. To override it:

```bash
ORCHESTRA_WAV_PORT=9000 /tmp/update_wavs.sh SERVER_IP -d Strings
```

## Changing the default WAV manually

Edit:

```text
~/NoBlackBoxes/LastBlackBox/boxes/audio/signal-processing/python/generation/default_wav.txt
```

and enter the filename of a WAV in the `wav/` directory, for example:

```text
Piano.wav
```

## Reconfiguring Chrony

If the orchestra server IP changes:

```bash
curl -fsSL https://raw.githubusercontent.com/lumar1729/nbb-orchestra/main/setup_chrony.sh -o /tmp/setup_chrony.sh \
&& chmod +x /tmp/setup_chrony.sh \
&& sudo /tmp/setup_chrony.sh NEW_SERVER_IP
```

Verify synchronization with:

```bash
chronyc tracking
chronyc sources -v
```

## Repository files

- `install_orchestra.sh` — complete initial Pi setup
- `install_orchestra_code.sh` — installs/updates Pi-side orchestra code
- `setup_chrony.sh` — configures time synchronization
- `update_wavs.sh` — downloads/replaces the WAV library
- `message_board_server.py` — server, synchronization, and assignment coordinator
- `pi_message_client.py` — Pi-side message/orchestra client
- `assign_wavs.py` — negotiates and stores orchestra parts
- `play_wav.py` — plays the Pi's assigned WAV
