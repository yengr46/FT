# FTAPPS — Installation Guide

## Requirements

- Windows 10 or 11 (64-bit)
- Python 3.11 **64-bit** — [download here](https://www.python.org/downloads/release/python-3119/)
- VLC media player **64-bit** — [download here](https://www.videolan.org/vlc/) *(required for FTVideo only)*

---

## Step 1 — Install Python 3.11 (64-bit)

1. Go to https://www.python.org/downloads/release/python-3119/
2. Download **Windows installer (64-bit)**
3. Run the installer
4. **Important:** tick **"Add Python to PATH"** before clicking Install
5. Click Install Now

To verify, open Command Prompt and run both checks:

```
python --version
```
Should show `Python 3.11.x`

```
python -c "import struct; print(struct.calcsize('P')*8)"
```
Must print `64`. If it prints `32` you have the wrong installer — uninstall and reinstall using the **Windows installer (64-bit)** link above.

---

## Step 2 — Install VLC (64-bit)

Required for FTVideo (video playback). Skip if you only need the photo/document tools.

1. Go to https://www.videolan.org/vlc/
2. Download and install the **64-bit** version
3. Must be 64-bit to match Python

---

## Step 3 — Get the FTAPPS code

Copy the FTAPPS folder to your PC. The folder structure must be:

```
FTAPPS_Cowork\
    main\
        FTMenu.py
        FTmod.py
    helpers\
        FTView.py
        FTVideo.py
        FTMap.py
        FTCompare.py
        FTFiler.py
        FTImgedit.py
    libraries\
        ft_movie.py
        ft_combine_strip.py
        ft_file_ops.py
        ... (other library files)
    requirements.txt
    setup.bat
    run_ftmenu.bat
```

---

## Step 4 — Install Python packages

Double-click **`setup.bat`** in the FTAPPS folder.

This installs all required Python packages automatically. You should see each package install successfully.

Or run manually from Command Prompt:
```
pip install -r requirements.txt
```

---

## Step 5 — Launch FTAPPS

Double-click **`run_ftmenu.bat`** to open the launcher.

Or from Command Prompt:
```
python main\FTMenu.py
```

On first launch you will be prompted to create a project and set your root folder(s).

---

## Troubleshooting

**`python` not recognised**
Python was not added to PATH during install. Re-run the Python installer, choose Modify, and tick "Add Python to PATH".

**`ModuleNotFoundError: No module named 'vlc'`**
VLC is not installed, or you installed the 32-bit version. Install VLC 64-bit.

**`ModuleNotFoundError: No module named 'cv2'`**
Run `pip install opencv-python` in Command Prompt.

**Video plays audio but no picture**
VLC version mismatch. Uninstall VLC and reinstall the 64-bit version matching your Python architecture.

**Blank window or crash on launch**
Check that your Python is 3.11 **64-bit** — run `python -c "import struct; print(struct.calcsize('P')*8)"` and confirm it prints `64`.

---

## Package versions (reference)

| Package | Minimum version |
|---------|----------------|
| Pillow | 10.0.0 |
| opencv-python | 4.8.0 |
| numpy | 1.24.0 |
| pygame | 2.5.0 |
| PyMuPDF | 1.23.0 |
| piexif | 1.1.3 |
| fpdf2 | 2.7.0 |
| python-vlc | 3.0.18122 |
