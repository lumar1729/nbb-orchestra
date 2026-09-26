# nbb-orchestra

Deployment tools for coordinating musical Raspberry Pi robots in the No
Black Boxes course.

The main No Black Boxes repository must already be installed on each Pi
at:

``` text
~/NoBlackBoxes/LastBlackBox
```

## Ports

-   **8000** --- No Black Boxes message-board/orchestra server
-   **8001** --- WAV file server

## Server setup

Run the normal No Black Boxes message-board server on the laptop as
usual.

Store the orchestra WAV files in one directory and serve it separately
on port 8001:

``` powershell
python -m http.server 8001 --directory "C:\path\to\orchestra\wav"
```

Leave this running while installing Pis or updating their WAV libraries.
If Windows Firewall prompts for access, allow Python on the
private/local network.

Find the server's IPv4 address with:

``` powershell
ipconfig
```

You can test the WAV server from a Pi with:

``` bash
curl http://SERVER_IP:8001/
```

## Initial Pi installation

First install the main No Black Boxes repository. Then, with the WAV
server running, run:

``` bash
curl -fsSL https://raw.githubusercontent.com/lumar1729/nbb-orchestra/main/install_orchestra.sh -o /tmp/install_orchestra.sh \
&& chmod +x /tmp/install_orchestra.sh \
&& sudo /tmp/install_orchestra.sh SERVER_IP
```

For example:

``` bash
sudo /tmp/install_orchestra.sh 192.168.1.115
```

You can optionally choose the Pi's initial default WAV:

``` bash
sudo /tmp/install_orchestra.sh 192.168.1.115 -d Choir.wav
```

`--default Choir` is equivalent. If no default is supplied, the first
WAV alphabetically is used.

The installer:

1.  Checks that `LastBlackBox` exists.
2.  Installs required system packages.
3.  Installs the orchestra Python code and acoustic-localisation assets.
4.  Configures Chrony against the server.
5.  Downloads the musical WAV library from port 8001.
6.  Writes the selected WAV to `default_wav.txt`.

## Installed files

``` text
~/pi_message_client.py

~/NoBlackBoxes/LastBlackBox/boxes/audio/signal-processing/python/generation/
├── play_wav.py
├── assign_wavs.py
├── binaural_chirp_test.py
├── generate_localisation_chirp.py
├── localisation_chirp.wav
├── default_wav.txt
└── wav/
    └── *.wav
```

`play_wav.py` normally plays the filename stored in `default_wav.txt`.
The localisation chirp is stored separately as
`generation/localisation_chirp.wav`, so it is not part of the musical
WAV library.

## Starting the message client

``` bash
~/NoBlackBoxes/LastBlackBox/_tmp/LBB/bin/python ~/pi_message_client.py SERVER_IP
```

For example:

``` bash
~/NoBlackBoxes/LastBlackBox/_tmp/LBB/bin/python ~/pi_message_client.py 192.168.1.115
```

## Assigning orchestra parts

There are two assignment modes.

### Random negotiated assignment

With the desired Pis connected, use the server's **Run command in sync**
control to run:

``` text
python ~/NoBlackBoxes/LastBlackBox/boxes/audio/signal-processing/python/generation/assign_wavs.py
```

Target `all`, or select the Pis that should participate.

The server creates a random speaking order. Each Pi independently
assigns preference scores to the available WAVs and, in turn, proposes
its favourite remaining part on the public message board. Other Pis
whose current favourite is the same part can respond before the
proposing Pi claims it.

The process stops when every Pi has a part or every WAV has been
assigned. Assigned Pis write their result to `default_wav.txt`.

### Spatial orchestral assignment

The server can instead infer the relative 2-D arrangement of the robots
from binaural interaural time differences (ITDs), then use the recovered
arrangement to assign parts according to approximate orchestral seating.

In the server's **Run command in sync** control, run:

``` text
spatially_assign_wavs
```

Target `all`, or select the Pis that should participate.

The optional `-v` / `--volume` argument controls localisation-chirp
volume from 0 to 100:

``` text
spatially_assign_wavs -v 70
```

The default is 100.

During localisation, the participating Pis take turns emitting
`generation/localisation_chirp.wav`. For each source Pi, every other Pi
records the chirp through its two microphones using
`binaural_chirp_test.py` and estimates a signed ITD. The server waits
until the listeners report that their microphones are ready before
allowing the synchronized chirp. A source round is retried if required
measurements are missing or fail the quality checks.

The server converts the directed ITDs into the dimensionless matrix

``` text
Q[i,j] = speed_of_sound * ITD[i,j] / ear_spacing
```

and feeds it to `itd_solver.py`. The solver uses the complete set of
directed measurements to infer relative 2-D robot positions and
microphone headings. ITD does not provide absolute inter-robot distance,
so the recovered map is scale-free.

The best spatial solution is displayed on the server website.
`spatially_assign_wavs.py` then maps the available musical WAVs onto the
inferred arrangement using approximate orchestral seating. It recognizes
individual instruments as well as combined stems such as `Strings`,
`Woodwinds`, `Brass`, and `Choir`. Unrecognized MIDI-derived names are
still assignable.

Each Pi is told its assigned role through the public chat. Its client
then writes the assigned WAV filename to `default_wav.txt`, so the next
synchronized `play_wav.py` run automatically plays the new part.

Binaural ITD has a global reflection ambiguity: a configuration and its
mirror image produce equivalent measurements. The software therefore
chooses a deterministic orientation for display and assignment, but
without an external directional reference the displayed left/right
orientation cannot be guaranteed to correspond to physical
stage-left/stage-right.

### Unequal numbers of Pis and WAVs

Both assignment methods handle unequal numbers of Pis and musical WAV
files.

If there are more WAVs than Pis, some WAVs remain unused. If there are
more Pis than WAVs, the excess Pis sit out, post:

``` text
Bye chat!
```

and disconnect from the server. Restart `pi_message_client.py` when you
want a sitting-out Pi to reconnect.

After either assignment method, run `play_wav.py` in sync as usual. Each
connected Pi will play its newly assigned default WAV.

## Updating orchestra code

To update the Pi-side orchestra code and localisation assets
(`pi_message_client.py`, `play_wav.py`, `assign_wavs.py`,
`binaural_chirp_test.py`, `generate_localisation_chirp.py`, and
`localisation_chirp.wav`):

``` bash
curl -fsSL https://raw.githubusercontent.com/lumar1729/nbb-orchestra/main/install_orchestra_code.sh -o /tmp/install_orchestra_code.sh \
&& chmod +x /tmp/install_orchestra_code.sh \
&& /tmp/install_orchestra_code.sh
```

Existing versions are backed up as `.backup` before replacement.

## Updating the WAV library

With the WAV server running:

``` bash
curl -fsSL https://raw.githubusercontent.com/lumar1729/nbb-orchestra/main/update_wavs.sh -o /tmp/update_wavs.sh \
&& chmod +x /tmp/update_wavs.sh \
&& /tmp/update_wavs.sh SERVER_IP
```

The new library is downloaded to a temporary directory first and
replaces the existing library only after all files download
successfully.

To update the library and set a new default WAV at the same time:

``` bash
/tmp/update_wavs.sh SERVER_IP -d Strings.wav
```

`--default Strings` is equivalent; the `.wav` extension is optional. The
requested file is checked against the newly downloaded library before
anything is replaced. If it is not found, the existing WAV library and
default are left unchanged. If `-d`/`--default` is omitted, the existing
`default_wav.txt` is left unchanged.

The default WAV-server port is 8001. To override it:

``` bash
ORCHESTRA_WAV_PORT=9000 /tmp/update_wavs.sh SERVER_IP -d Strings
```

## Changing the default WAV manually

Edit:

``` text
~/NoBlackBoxes/LastBlackBox/boxes/audio/signal-processing/python/generation/default_wav.txt
```

and enter the filename of a WAV in the `wav/` directory, for example:

``` text
Piano.wav
```

## Reconfiguring Chrony

If the orchestra server IP changes:

``` bash
curl -fsSL https://raw.githubusercontent.com/lumar1729/nbb-orchestra/main/setup_chrony.sh -o /tmp/setup_chrony.sh \
&& chmod +x /tmp/setup_chrony.sh \
&& sudo /tmp/setup_chrony.sh NEW_SERVER_IP
```

Verify synchronization with:

``` bash
chronyc tracking
chronyc sources -v
```

## Repository files

-   `install_orchestra.sh` --- complete initial Pi setup
-   `install_orchestra_code.sh` --- installs/updates Pi-side orchestra
    code
-   `setup_chrony.sh` --- configures time synchronization
-   `update_wavs.sh` --- downloads/replaces the WAV library
-   `message_board_server.py` --- server, synchronization,
    acoustic-localisation, and assignment coordinator
-   `pi_message_client.py` --- Pi-side message/orchestra client
-   `assign_wavs.py` --- negotiates and stores randomly assigned
    orchestra parts
-   `spatially_assign_wavs.py` --- maps inferred robot positions to
    orchestral parts
-   `itd_solver.py` --- infers a scale-free 2-D robot arrangement from
    the directed ITD/Q matrix
-   `binaural_chirp_test.py` --- records a localisation chirp and
    estimates signed binaural ITD
-   `generate_localisation_chirp.py` --- regenerates the deterministic
    localisation chirp
-   `localisation_chirp.wav` --- chirp used for acoustic localisation
-   `play_wav.py` --- plays the Pi's assigned WAV and provides
    synchronized chirp playback
