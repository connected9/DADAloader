# DADAloader
# Async DADAloader 🚀 BY : RIFAT

Async DADAloader is a GUI-based asynchronous download manager built with Python, Tkinter, and powered by `aria2c`. It provides a user-friendly interface to manage multiple downloads efficiently, leveraging the power of `aria2c` for fast, multi-connection, and resumable downloads.

![Screenshot (Placeholder - you might want to add one)](https://via.placeholder.com/800x600.png?text=Async+DADAloader+UI+Screenshot)
*(Replace the placeholder above with an actual screenshot of your application)*

## ✨ Features

*   **User-friendly Graphical Interface:** Built with Tkinter for easy interaction.
*   **Powerful Downloading with `aria2c`:**
    *   Utilizes `aria2c` for multi-connection, segmented downloads.
    *   Supports resumable downloads.
*   **Automatic `aria2c` Setup:** If `aria2c` is not found (on Windows), the application will attempt to download and set it up automatically.
*   **Asynchronous Operations:** Uses `asyncio` to keep the UI responsive even while multiple downloads are in progress.
*   **Download Queue Management:**
    *   Add new downloads via URL.
    *   View a list of ongoing and completed downloads.
*   **Download Controls:**
    *   Start, Pause, Resume, and Stop individual downloads.
    *   Delete downloads from the list (and optionally the downloaded file).
*   **Real-time Progress Tracking:**
    *   Displays filename, total size (MB).
    *   Shows progress percentage.
    *   Live download speed (Mb/s).
    *   Estimated Time Remaining (ETA in seconds).
    *   Current status (Pending, Downloading, Paused, Completed, Error, Stopped).
*   **Persistent Download History:** Saves download information (URL, save path, progress, status) in an SQLite database (`async_dadaloader.db`), allowing downloads to be managed across sessions.
*   **Clipboard Monitoring:** Automatically detects valid URLs copied to the clipboard and prompts the user if they want to add them as a download.
*   **Detailed File Information:** Double-click a download to see a detailed information dialog with progress, downloaded amount, and time left.
*   **Organized Downloads:**
    *   Saves files to a default directory: `~/Downloads/AsyncDADAloader/`.
    *   Automatically generates unique filenames to avoid overwriting.
*   **Logging:** Logs application activity and errors to `async_dadaloader.log` for debugging.

## ⚙️ Requirements

*   **Python 3.7+**
*   **`aria2c` command-line download utility:**
    *   For **Windows**: The script will attempt to download `aria2c.exe` automatically if it's not found in the script's directory or system PATH.
    *   For **Linux/macOS**: You need to install `aria2c` manually.
        *   Debian/Ubuntu: `sudo apt install aria2`
        *   Fedora: `sudo dnf install aria2`
        *   macOS (using Homebrew): `brew install aria2`
        *   Or download from [aria2 GitHub Releases](https://github.com/aria2/aria2/releases).
*   **Python Libraries:**
    *   `requests`
    *   `pyperclip`
    *   `validators`

## 🛠️ Installation & Setup

1.  **Clone the repository or download the script:**
    ```bash
    git clone <your-repo-url> # Or just save the script as async_dadaloader.py
    cd <your-repo-directory>
    ```

2.  **Create a virtual environment (recommended):**
    ```bash
    python -m venv venv
    source venv/bin/activate  # On Windows: venv\Scripts\activate
    ```

3.  **Install required Python packages:**
    ```bash
    pip install requests pyperclip validators
    ```

4.  **Ensure `aria2c` is available:**
    *   **Windows:** If you don't have `aria2c.exe` in the same directory as the script or in your system PATH, the script will try to download it on first run (requires an internet connection).
    *   **Linux/macOS:** Install `aria2c` using your package manager as described in the "Requirements" section.

## 🚀 Usage

1.  **Run the script:**
    ```bash
    python async_dadaloader.py
    ```

2.  **Adding Downloads:**
    *   Click the "**Add Download**" button. A dialog will appear.
    *   Enter the **URL** of the file you want to download.
    *   Specify the **Save Path**. A default path and filename will be suggested (in `~/Downloads/AsyncDADAloader/`). You can browse to choose a different location/name.
    *   Click "**OK**".
    *   Alternatively, if you copy a valid URL to your clipboard, the application will detect it and ask if you want to add it as a download.

3.  **Managing Downloads:**
    *   The main window displays a table of all downloads.
    *   **Select a download** from the table.
    *   **Start/Pause/Resume:** Use the dynamic button (labeled "Start", "Pause", or "Resume" based on the selected download's status) to control the download.
    *   **Delete:** Click the "**Delete**" button to remove the selected download from the list. You'll be asked to confirm if you also want to delete the downloaded file from your disk.
    *   **View Details:** Double-click on a download entry in the table to open the "File Information" dialog, showing more detailed progress.

4.  **Status Bar:** The status bar at the bottom provides feedback on actions and general status.

5.  **Logs:** Check `async_dadaloader.log` for detailed activity and troubleshooting.

## 🗂️ File Structure

When you run the script, the following files/folders might be created in the same directory as the script (or user's home for some):

*   `async_dadaloader.py`: The main Python script.
*   `async_dadaloader.db`: SQLite database storing download history and states.
*   `async_dadaloader.log`: Log file for application events and errors.
*   `aria2c.exe` (Windows only): If downloaded automatically by the script.
*   `~/Downloads/AsyncDADAloader/`: Default directory where downloaded files are saved.

## 🔩 How It Works (Briefly)

*   The GUI is managed by **Tkinter**.
*   `AsyncDownloader` class interfaces with the **`aria2c`** command-line tool using `asyncio.create_subprocess_exec`.
*   Output from `aria2c` (stdout) is parsed in real-time to update download progress, speed, and ETA.
*   `DownloadItem` class represents individual download tasks and their state.
*   `DatabaseManager` class handles all SQLite operations for persisting download data.
*   `asyncio` is used for non-blocking I/O operations, ensuring the UI remains responsive.
*   `pyperclip` is used to monitor the system clipboard for URLs.
*   `validators` helps in quickly checking if a string from the clipboard is a valid URL.

## 📜 License

This project is unlicensed (or specify your license, e.g., MIT License).

## 🤝 Contributing

Contributions, issues, and feature requests are welcome!

---

This README should provide a good overview and usage instructions for your script!
