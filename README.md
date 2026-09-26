# nbb-orchestra

Deployment scripts for enabling musical coordination between robots in the No Black Boxes course.

The main No Black Boxes repository must already be installed on each Raspberry Pi at:

```text
~/NoBlackBoxes/LastBlackBox
```

This repository installs the orchestra-specific Python files, configures Chrony time synchronisation, and downloads the current WAV library from the orchestra server over the local network.

## Ports

The setup uses two separate HTTP ports:

- **8000** — the existing No Black Boxes website/server used by `pi_message_client.py`
- **8001** — the WAV file server used by `update_wavs.sh`

## Server setup

The orchestra server laptop must run the normal No Black Boxes server as usual and, separately, serve the WAV library over HTTP on port **8001**.

### 1. Create the WAV directory

Put all WAV files that should be installed on the Pis in one directory, for example:

```text
C:\Users\<username>\NoBlackBoxes\orchestra\wav\
    kick.wav
    snare.wav
    melody.wav
```

### 2. Start the WAV server

On Windows, open PowerShell or Command Prompt and run:

```powershell
python -m http.server 8001 --directory "C:\path\to\orchestra\wav"
```

For example:

```powershell
python -m http.server 8001 --directory "C:\Users\<username>\NoBlackBoxes\orchestra\wav"
```

Leave this process running while Pis are being installed or while their WAV libraries are being updated.

If Windows Firewall prompts for access, allow Python on the private/local network.

### 3. Test the WAV server

From another computer, open:

```text
http://SERVER_IP:8001/
```

Or from a Pi:

```bash
curl http://SERVER_IP:8001/
```

You should see a directory listing containing the WAV files.

### 4. Find the server IP

On Windows:

```powershell
ipconfig
```

Find the IPv4 address of the adapter connected to the same network as the Pis, for example:

```text
192.168.1.115
```

Use that address as `SERVER_IP` below.

## Initial Raspberry Pi installation

First install/clone the main No Black Boxes repository normally. The orchestra installer assumes this directory already exists:

```text
~/NoBlackBoxes/LastBlackBox
```

Also make sure the server's WAV server on port 8001 is running.

Then run this on the Pi:

```bash
curl -fsSL https://raw.githubusercontent.com/lumar1729/nbb-orchestra/main/install_orchestra.sh -o /tmp/install_orchestra.sh \
&& chmod +x /tmp/install_orchestra.sh \
&& sudo /tmp/install_orchestra.sh SERVER_IP
```

For example:

```bash
curl -fsSL https://raw.githubusercontent.com/lumar1729/nbb-orchestra/main/install_orchestra.sh -o /tmp/install_orchestra.sh \
&& chmod +x /tmp/install_orchestra.sh \
&& sudo /tmp/install_orchestra.sh 192.168.1.115
```

### Choosing the default WAV

The installer can optionally assign a default WAV file to the Pi with `-d` or `--default`.

For example:

```bash
curl -fsSL https://raw.githubusercontent.com/lumar1729/nbb-orchestra/main/install_orchestra.sh -o /tmp/install_orchestra.sh \
&& chmod +x /tmp/install_orchestra.sh \
&& sudo /tmp/install_orchestra.sh 192.168.1.115 -d Choir.wav
```

The `.wav` extension is optional, so this is equivalent:

```bash
sudo /tmp/install_orchestra.sh 192.168.1.115 --default Choir
```

The requested filename is matched case-insensitively against the WAV files downloaded from the server. If the requested WAV does not exist, installation stops and prints the available WAV filenames.

If `-d` / `--default` is not provided, the installer selects the first `.wav` file alphabetically.

The selected filename is stored on the Pi in:

```text
~/NoBlackBoxes/LastBlackBox/boxes/audio/signal-processing/python/generation/default_wav.txt
```

`play_wav.py` reads this file when it starts. If `default_wav.txt` is absent or empty, `play_wav.py` also falls back to the first WAV alphabetically. This keeps the playback code identical across Pis while allowing each Pi to have its own assigned default instrument or role.

The installer:

1. Checks that `LastBlackBox` already exists.
2. Installs missing `curl`, `chrony`, and `python3` system packages.
3. Downloads the current deployment scripts from this repository.
4. Installs `pi_message_client.py` and `play_wav.py`.
5. Configures Chrony to synchronise with the specified server.
6. Downloads the current WAV library from `SERVER_IP:8001`.
7. Selects the requested default WAV, or the first WAV alphabetically if no default was specified, and writes it to `default_wav.txt`.

The installer detects the invoking user's home directory, so it does not depend on the username being `lucarakowski`.

## Installed locations

`pi_message_client.py`:

```text
~/pi_message_client.py
```

`play_wav.py`:

```text
~/NoBlackBoxes/LastBlackBox/boxes/audio/signal-processing/python/generation/play_wav.py
```

WAV library:

```text
~/NoBlackBoxes/LastBlackBox/boxes/audio/signal-processing/python/generation/wav/
```

Default WAV configuration:

```text
~/NoBlackBoxes/LastBlackBox/boxes/audio/signal-processing/python/generation/default_wav.txt
```

For example, a Pi assigned to the choir part might contain:

```text
Choir.wav
```

## Updating orchestra code

To install the latest `pi_message_client.py` and `play_wav.py`:

```bash
curl -fsSL https://raw.githubusercontent.com/lumar1729/nbb-orchestra/main/install_orchestra_code.sh -o /tmp/install_orchestra_code.sh \
&& chmod +x /tmp/install_orchestra_code.sh \
&& /tmp/install_orchestra_code.sh
```

Existing versions are backed up as `.backup` before replacement.

## Updating the WAV library

Make sure the server is running:

```powershell
python -m http.server 8001 --directory "C:\path\to\orchestra\wav"
```

Then on the Pi:

```bash
curl -fsSL https://raw.githubusercontent.com/lumar1729/nbb-orchestra/main/update_wavs.sh -o /tmp/update_wavs.sh \
&& chmod +x /tmp/update_wavs.sh \
&& /tmp/update_wavs.sh SERVER_IP
```

For example:

```bash
curl -fsSL https://raw.githubusercontent.com/lumar1729/nbb-orchestra/main/update_wavs.sh -o /tmp/update_wavs.sh \
&& chmod +x /tmp/update_wavs.sh \
&& /tmp/update_wavs.sh 192.168.1.115
```

The new library is downloaded into a temporary directory first. The existing library is replaced only after all new WAV files have downloaded successfully.

The default WAV port is 8001. It can be overridden if necessary:

```bash
ORCHESTRA_WAV_PORT=9000 /tmp/update_wavs.sh SERVER_IP
```

## Changing a Pi's default WAV

The default can be changed without reinstalling the orchestra software. Edit:

```text
~/NoBlackBoxes/LastBlackBox/boxes/audio/signal-processing/python/generation/default_wav.txt
```

and set it to the filename of one of the WAV files in the `wav/` directory, for example:

```text
Piano.wav
```

This per-Pi configuration is intended to make it straightforward to add server-assigned roles later without changing `play_wav.py` on each Pi.

## Reconfiguring Chrony

If the orchestra server IP changes:

```bash
curl -fsSL https://raw.githubusercontent.com/lumar1729/nbb-orchestra/main/setup_chrony.sh -o /tmp/setup_chrony.sh \
&& chmod +x /tmp/setup_chrony.sh \
&& sudo /tmp/setup_chrony.sh NEW_SERVER_IP
```

Check synchronisation with:

```bash
chronyc tracking
chronyc sources -v
```

## Starting the message client

After installation:

```bash
/home/yourname/NoBlackBoxes/LastBlackBox/_tmp/LBB/bin/python /home/yourname/pi_message_client.py SERVER_IP
```

For example:

```bash
/home/yourname/NoBlackBoxes/LastBlackBox/_tmp/LBB/bin/python /home/yourname/pi_message_client.py 192.168.1.115
```

## Repository files

- `install_orchestra.sh` — complete initial Pi installation
- `install_orchestra_code.sh` — installs/updates the orchestra Python code
- `setup_chrony.sh` — configures time synchronisation
- `update_wavs.sh` — downloads/replaces the WAV library
- `pi_message_client.py` — Pi-side message/orchestra client
- `play_wav.py` — WAV playback script
