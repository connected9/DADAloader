import sys
import os
import time
import logging
import sqlite3
import shutil
import zipfile
import requests
from typing import Optional, List, Tuple
import asyncio
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import pyperclip
import validators
from urllib.parse import urlparse

# Setup logging
logging.basicConfig(
    filename='async_dadaloader.log',
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

class DownloadItem:
    """Represents a single download task."""
    def __init__(self, url: str, save_path: str, file_size: int = 0):
        self.url = url
        self.save_path = save_path
        self.file_size = file_size
        self.downloaded = 0
        self.status = "Pending"
        self.speed = 0.0
        self.eta = 0
        self.progress = 0.0
        self.is_paused = False
        self.is_stopped = False
        self.process = None  # For aria2c subprocess
        self.start_time = None
        self.task = None  # For asyncio task
        logging.debug(f"Created DownloadItem for {url}")

class DatabaseManager:
    """Handles SQLite database operations for download history."""
    def __init__(self, db_name: str = "async_dadaloader.db"):
        self.conn = sqlite3.connect(db_name)
        self.create_tables()

    def create_tables(self):
        with self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS downloads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT NOT NULL,
                    save_path TEXT NOT NULL,
                    file_size INTEGER,
                    status TEXT,
                    progress REAL,
                    downloaded INTEGER DEFAULT 0,
                    is_paused INTEGER DEFAULT 0
                )
            """)

    def add_download(self, download: DownloadItem):
        with self.conn:
            self.conn.execute(
                "INSERT INTO downloads (url, save_path, file_size, status, progress, downloaded, is_paused) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (download.url, download.save_path, download.file_size, download.status, download.progress, download.downloaded, int(download.is_paused))
            )

    def update_download(self, download_id: int, progress: float, status: str, downloaded: int, is_paused: bool):
        with self.conn:
            self.conn.execute(
                "UPDATE downloads SET progress = ?, status = ?, downloaded = ?, is_paused = ? WHERE id = ?",
                (progress, status, downloaded, int(is_paused), download_id)
            )

    def get_downloads(self) -> List[Tuple]:
        with self.conn:
            return self.conn.execute("SELECT id, url, save_path, file_size, status, progress, downloaded, is_paused FROM downloads").fetchall()

    def delete_download(self, download_id: int):
        with self.conn:
            self.conn.execute("DELETE FROM downloads WHERE id = ?", (download_id,))

class AsyncDownloader:
    """Handles download tasks using aria2c."""
    def __init__(self, window):
        self.window = window
        self.overhead_factor = 0.1  # Assume 10% network overhead
        self.aria2c_path = self.ensure_aria2c()

    def ensure_aria2c(self) -> str:
        """Ensure aria2c is available, downloading it if necessary."""
        script_dir = os.path.dirname(os.path.abspath(__file__))
        aria2c_path = os.path.join(script_dir, "aria2c.exe")
        
        if os.path.isfile(aria2c_path):
            logging.debug(f"Found aria2c at: {aria2c_path}")
            return aria2c_path
        if shutil.which("aria2c"):
            return shutil.which("aria2c")

        try:
            messagebox.showinfo("Info", "aria2c not found. Downloading now...")
            logging.info("aria2c not found, initiating download")
            url = "https://github.com/aria2/aria2/releases/download/release-1.37.0/aria2-1.37.0-win-64bit-build1.zip"
            response = requests.get(url, stream=True)
            response.raise_for_status()
            
            zip_path = os.path.join(script_dir, "aria2c.zip")
            with open(zip_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extract("aria2-1.37.0-win-64bit-build1/aria2c.exe", script_dir)
            
            os.rename(
                os.path.join(script_dir, "aria2-1.37.0-win-64bit-build1/aria2c.exe"),
                aria2c_path
            )
            os.remove(zip_path)
            os.rmdir(os.path.join(script_dir, "aria2-1.37.0-win-64bit-build1"))
            
            logging.debug(f"Downloaded and extracted aria2c to: {aria2c_path}")
            messagebox.showinfo("Success", "aria2c downloaded successfully.")
            return aria2c_path
        except Exception as e:
            logging.error(f"Failed to download aria2c: {str(e)}")
            raise RuntimeError(
                "Failed to download aria2c. Please ensure an internet connection and try again, or manually install aria2c from https://aria2.github.io/."
            )

    def parse_size(self, size_str: str) -> int:
        """Parse size string (e.g., '1.9GiB') to bytes."""
        size_str = size_str.strip()
        units = {'KiB': 1024, 'MiB': 1024**2, 'GiB': 1024**3}
        for unit, multiplier in units.items():
            if unit in size_str:
                value = float(size_str.replace(unit, ""))
                return int(value * multiplier)
        return int(float(size_str))

    def parse_eta(self, eta_str: str) -> int:
        """Parse ETA string (e.g., '15s', '1h38m7') to seconds with error handling."""
        eta_str = eta_str.replace("ETA:", "").replace("s]", "").strip()
        total_seconds = 0

        try:
            if 'h' in eta_str or 'm' in eta_str:
                parts = eta_str
                hours = 0
                minutes = 0
                seconds = 0

                if 'h' in parts:
                    h_part, parts = parts.split('h')
                    hours = int(h_part) if h_part else 0

                if 'm' in parts:
                    m_part, parts = parts.split('m')
                    minutes = int(m_part) if m_part else 0

                seconds = int(parts) if parts else 0
                total_seconds = (hours * 3600) + (minutes * 60) + seconds
            else:
                total_seconds = int(eta_str)
        except (ValueError, IndexError):
            logging.error(f"Failed to parse ETA: {eta_str}, defaulting to 0 seconds")
            total_seconds = 0

        return total_seconds

    async def download(self, download_id: int, download: DownloadItem):
        logging.debug(f"Starting download for ID {download_id}: {download.url}")
        try:
            cmd = [
                self.aria2c_path,
                "-x", "16",  # Maximum number of connections per server
                "-s", "16",  # Number of segments
                "--dir", os.path.dirname(download.save_path),
                "--out", os.path.basename(download.save_path),
                "--continue=true",  # Ensure resume capability
                "--summary-interval=1",  # Update every second for parsing
                download.url
            ]
            download.start_time = time.time()
            download.status = "Downloading"
            self.window.update_download_progress(download_id, download.progress, 0.0, 0.0, "Downloading")

            while not download.is_stopped:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                download.process = process

                while True:
                    line = await process.stdout.readline()
                    if not line:
                        break
                    line = line.decode().strip()
                    if download.is_stopped:
                        process.terminate()
                        download.status = "Stopped"
                        self.window.update_download_progress(download_id, download.progress, 0.0, 0.0, "Stopped")
                        break
                    if download.is_paused:
                        process.terminate()
                        download.status = "Paused"
                        self.window.update_download_progress(download_id, download.progress, 0.0, 0.0, "Paused")
                        break

                    if "[#" in line and "ETA:" in line:
                        parts = line.split()
                        for part in parts:
                            if "/" in part and "(" in part:
                                downloaded_str, total_str = part.split("/")
                                download.downloaded = self.parse_size(downloaded_str)
                                total_str = total_str.split("(")[0]
                                download.file_size = self.parse_size(total_str)
                                if download.file_size > 0:
                                    download.progress = (download.downloaded / download.file_size) * 100
                            if "DL:" in part:
                                speed_str = part.replace("DL:", "")
                                speed_bytes = self.parse_size(speed_str)
                                download.speed = (speed_bytes * 8) / (1024 * 1024)
                            if "ETA:" in part:
                                download.eta = self.parse_eta(part)
                        self.window.update_download_progress(download_id, download.progress, download.speed, download.eta, "Downloading")

                await process.wait()
                if download.is_paused:
                    while download.is_paused and not download.is_stopped:
                        await asyncio.sleep(1)
                    if download.is_stopped:
                        download.status = "Stopped"
                        self.window.update_download_progress(download_id, download.progress, 0.0, 0.0, "Stopped")
                        break
                    download.status = "Downloading"
                    self.window.update_download_progress(download_id, download.progress, download.speed, download.eta, "Downloading")
                    continue

                if process.returncode == 0:
                    download.status = "Completed"
                    download.progress = 100.0
                    self.window.update_download_progress(download_id, 100.0, 0.0, 0.0, "Completed")
                    break
                else:
                    download.status = "Error"
                    self.window.update_download_progress(download_id, download.progress, 0.0, 0.0, "Error")
                    self.window.show_error("Download failed")
                    break

        except Exception as e:
            logging.error(f"Download error for {download.url}: {str(e)}")
            download.status = "Error"
            self.window.show_error(f"Download failed: {str(e)}")
            self.window.update_download_progress(download_id, download.progress, 0.0, 0.0, "Error")

class FileInfoDialog(tk.Toplevel):
    """Dialog to show file information and download progress."""
    def __init__(self, parent, download_id: int, download: DownloadItem):
        super().__init__(parent)
        self.title("File Information")
        self.download_id = download_id
        self.download = download
        self.init_ui()
        self.update_info()
        self.center_dialog(parent)

    def init_ui(self):
        frame = ttk.Frame(self)
        frame.pack(padx=10, pady=10)

        ttk.Label(frame, text="Filename:").grid(row=0, column=0, sticky="w", padx=5, pady=2)
        self.filename_label = ttk.Label(frame, text=os.path.basename(self.download.save_path))
        self.filename_label.grid(row=0, column=1, sticky="w", padx=5, pady=2)

        size_mb = f"{self.download.file_size / (1024 * 1024):.2f} MB" if self.download.file_size else "Unknown"
        ttk.Label(frame, text="Total Size:").grid(row=1, column=0, sticky="w", padx=5, pady=2)
        self.size_label = ttk.Label(frame, text=size_mb)
        self.size_label.grid(row=1, column=1, sticky="w", padx=5, pady=2)

        ttk.Label(frame, text="Progress:").grid(row=2, column=0, sticky="w", padx=5, pady=2)
        self.progress_bar = ttk.Progressbar(frame, length=200, mode='determinate', style='green.Horizontal.TProgressbar')
        self.progress_bar.grid(row=2, column=1, padx=5, pady=2)

        ttk.Label(frame, text="Downloaded:").grid(row=3, column=0, sticky="w", padx=5, pady=2)
        self.downloaded_label = ttk.Label(frame, text="0.00 MB")
        self.downloaded_label.grid(row=3, column=1, sticky="w", padx=5, pady=2)

        ttk.Label(frame, text="Left:").grid(row=4, column=0, sticky="w", padx=5, pady=2)
        self.left_label = ttk.Label(frame, text="0.00 MB")
        self.left_label.grid(row=4, column=1, sticky="w", padx=5, pady=2)

        ttk.Label(frame, text="Time Left:").grid(row=5, column=0, sticky="w", padx=5, pady=2)
        self.time_left_label = ttk.Label(frame, text="0 s")
        self.time_left_label.grid(row=5, column=1, sticky="w", padx=5, pady=2)

        style = ttk.Style()
        style.configure("green.Horizontal.TProgressbar", troughcolor='white', background='green')

    def center_dialog(self, parent):
        self.update_idletasks()
        parent_width = parent.winfo_width()
        parent_height = parent.winfo_height()
        parent_x = parent.winfo_rootx()
        parent_y = parent.winfo_rooty()
        dialog_width = self.winfo_reqwidth()
        dialog_height = self.winfo_reqheight()
        x = parent_x + (parent_width - dialog_width) // 2
        y = parent_y + (parent_height - dialog_height) // 2
        self.geometry(f"+{x}+{y}")

    def update_info(self):
        if not self.winfo_exists():
            return
        size_mb = f"{self.download.file_size / (1024 * 1024):.2f} MB" if self.download.file_size else "Unknown"
        self.size_label.config(text=size_mb)

        self.progress_bar['value'] = self.download.progress

        downloaded_mb = self.download.downloaded / (1024 * 1024)
        left_mb = (self.download.file_size - self.download.downloaded) / (1024 * 1024) if self.download.file_size else 0
        self.downloaded_label.config(text=f"{downloaded_mb:.2f} MB")
        self.left_label.config(text=f"{left_mb:.2f} MB" if self.download.file_size else "Unknown")

        self.time_left_label.config(text=f"{self.download.eta} s")

        self.after(500, self.update_info)

class AddDownloadDialog(tk.Toplevel):
    """Dialog for adding a new download."""
    def __init__(self, parent, url: str = ""):
        super().__init__(parent)
        self.title("Add New Download")
        self.parent = parent
        self.result = None

        frame = ttk.Frame(self)
        frame.pack(padx=10, pady=10)

        ttk.Label(frame, text="URL:").grid(row=0, column=0, sticky="e")
        self.url_entry = ttk.Entry(frame, width=50)
        self.url_entry.insert(0, url)
        self.url_entry.grid(row=0, column=1, padx=5, pady=5)

        default_dir = os.path.expanduser("~/Downloads/AsyncDADAloader")
        os.makedirs(default_dir, exist_ok=True)
        default_filename = self.get_unique_filename(url) if url else "download"
        default_path = os.path.join(default_dir, default_filename)

        ttk.Label(frame, text="Save Path:").grid(row=1, column=0, sticky="e")
        self.path_entry = ttk.Entry(frame, width=50)
        self.path_entry.insert(0, default_path)
        self.path_entry.grid(row=1, column=1, padx=5, pady=5)

        ttk.Button(frame, text="Browse", command=self.browse_save_path).grid(row=1, column=2, padx=5)

        ttk.Button(frame, text="OK", command=self.on_ok).grid(row=2, column=0, columnspan=3, pady=5)
        ttk.Button(frame, text="Cancel", command=self.on_cancel).grid(row=3, column=0, columnspan=3)

        self.center_dialog(parent)
        self.transient(parent)
        self.grab_set()

    def get_filename_from_url(self, url: str) -> str:
        parsed_url = urlparse(url)
        filename = os.path.basename(parsed_url.path)
        return filename if filename else f"download_{int(time.time())}"

    def get_unique_filename(self, url: str) -> str:
        base_filename = self.get_filename_from_url(url)
        name, ext = os.path.splitext(base_filename)
        dir_path = os.path.expanduser("~/Downloads/AsyncDADAloader")
        counter = 1
        new_filename = base_filename

        while os.path.exists(os.path.join(dir_path, new_filename)):
            new_filename = f"{name}_{counter}{ext}"
            counter += 1

        return new_filename

    def browse_save_path(self):
        path = filedialog.asksaveasfilename(initialfile=self.path_entry.get())
        if path:
            self.path_entry.delete(0, tk.END)
            self.path_entry.insert(0, path)

    def on_ok(self):
        url = self.url_entry.get()
        path = self.path_entry.get()
        if validators.url(url) and path and os.access(os.path.dirname(path), os.W_OK):
            self.result = (url, path)
            self.destroy()
        else:
            messagebox.showerror("Error", "Invalid URL or save path")

    def on_cancel(self):
        self.result = None
        self.destroy()

    def center_dialog(self, parent):
        self.update_idletasks()
        parent_width = parent.winfo_width()
        parent_height = parent.winfo_height()
        parent_x = parent.winfo_rootx()
        parent_y = parent.winfo_rooty()
        dialog_width = self.winfo_reqwidth()
        dialog_height = self.winfo_reqheight()
        x = parent_x + (parent_width - dialog_width) // 2
        y = parent_y + (parent_height - dialog_height) // 2
        self.geometry(f"+{x}+{y}")

class AsyncDADAloaderWindow(tk.Tk):
    """Main application window using Tkinter and asyncio."""
    def __init__(self, loop):
        super().__init__()
        self.title("Async DADAloader")
        self.geometry("800x600")
        self.loop = loop
        self.db = DatabaseManager()
        self.downloads: dict = {}
        self.downloader = AsyncDownloader(self)
        self.last_clipboard = pyperclip.paste()
        self.init_ui()
        self.load_downloads()
        self.after(100, self.process_asyncio)
        self.after(100, self.check_clipboard)
        self.after(200, self.update_ui)

    def init_ui(self):
        self.table_frame = ttk.Frame(self)
        self.table_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        self.table_scroll = ttk.Scrollbar(self.table_frame, orient=tk.VERTICAL)
        self.table_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        self.table = ttk.Treeview(
            self.table_frame,
            columns=("Filename", "Size (MB)", "Progress (%)", "Avg Speed (Mb/s)", "ETA (s)", "Status"),
            show="headings",
            selectmode="browse",
            yscrollcommand=self.table_scroll.set
        )
        self.table.heading("Filename", text="Filename")
        self.table.heading("Size (MB)", text="Size (MB)")
        self.table.heading("Progress (%)", text="Progress (%)")
        self.table.heading("Avg Speed (Mb/s)", text="Avg Speed (Mb/s)")
        self.table.heading("ETA (s)", text="ETA (s)")
        self.table.heading("Status", text="Status")
        self.table.column("Filename", width=200)
        self.table.column("Size (MB)", width=100)
        self.table.column("Progress (%)", width=100)
        self.table.column("Avg Speed (Mb/s)", width=100)
        self.table.column("ETA (s)", width=100)
        self.table.column("Status", width=100)
        self.table.pack(fill=tk.BOTH, expand=True)
        self.table_scroll.config(command=self.table.yview)

        self.table.bind("<Double-1>", self.show_file_info_dialog)
        self.table.bind("<<TreeviewSelect>>", self.on_selection_change)

        button_frame = ttk.Frame(self)
        button_frame.pack(fill=tk.X, padx=10, pady=5)

        ttk.Button(button_frame, text="Add Download", command=lambda: self.show_add_download_dialog("")).pack(side=tk.LEFT, padx=5)
        self.toggle_button = ttk.Button(button_frame, text="Start", command=self.toggle_download)
        self.toggle_button.pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="Delete", command=self.delete_download).pack(side=tk.LEFT, padx=5)

        self.status_bar = ttk.Label(self, text="Ready", anchor="w")
        self.status_bar.pack(fill=tk.X, padx=10, pady=5)

    def show_file_info_dialog(self, event):
        selected = self.table.selection()
        if not selected:
            self.status_bar.config(text="No download selected")
            return
        for item in selected:
            download_id = int(self.table.item(item, "text"))
            download = self.downloads.get(download_id)
            if download:
                FileInfoDialog(self, download_id, download)

    def process_asyncio(self):
        try:
            self.loop.call_soon(lambda: None)
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            self.loop.run_until_complete(asyncio.sleep(0))
        except RuntimeError:
            pass
        self.after(100, self.process_asyncio)

    def check_clipboard(self):
        current_clipboard = pyperclip.paste()
        if current_clipboard != self.last_clipboard and validators.url(current_clipboard):
            self.last_clipboard = current_clipboard
            if messagebox.askyesno("URL Detected", f"Download {current_clipboard}?"):
                self.show_add_download_dialog(current_clipboard)
        self.after(100, self.check_clipboard)

    def show_add_download_dialog(self, url: str = ""):
        dialog = AddDownloadDialog(self, url)
        self.wait_window(dialog)
        if dialog.result:
            url, save_path = dialog.result
            self.add_download(url, save_path)
            self.status_bar.config(text=f"Added download: {os.path.basename(save_path)}")

    def add_download(self, url: str, save_path: str):
        download = DownloadItem(url, save_path)
        self.db.add_download(download)
        download_id = self.db.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        self.downloads[download_id] = download
        self.start_download(download_id, download)

    def start_download(self, download_id: int, download: DownloadItem):
        download.is_paused = False
        download.is_stopped = False
        download.task = self.loop.create_task(self.downloader.download(download_id, download))
        logging.debug(f"Scheduled download task for ID {download_id}")
        self.update_table()

    def toggle_download(self):
        selected = self.table.selection()
        if not selected:
            self.status_bar.config(text="No download selected")
            logging.debug("Toggle attempted but no item selected")
            return

        for item in selected:
            download_id = int(self.table.item(item, "text"))
            download = self.downloads[download_id]
            current_status = download.status

            if current_status in ["Pending", "Stopped", "Error", "Completed"]:
                download.status = "Downloading"
                download.is_paused = False
                download.is_stopped = False
                self.start_download(download_id, download)
                self.toggle_button.config(text="Pause")
                self.status_bar.config(text=f"Started download: {os.path.basename(download.save_path)}")
                logging.debug(f"Started download ID {download_id}")
            elif current_status == "Paused":
                download.is_paused = False
                download.status = "Downloading"
                self.db.update_download(download_id, download.progress, "Downloading", download.downloaded, download.is_paused)
                self.toggle_button.config(text="Pause")
                self.status_bar.config(text=f"Resumed download: {os.path.basename(download.save_path)}")
                logging.debug(f"Resumed download ID {download_id}")
            elif current_status == "Downloading":
                download.is_paused = True
                download.status = "Paused"
                self.db.update_download(download_id, download.progress, "Paused", download.downloaded, download.is_paused)
                self.toggle_button.config(text="Resume")
                self.status_bar.config(text=f"Paused download: {os.path.basename(download.save_path)}")
                logging.debug(f"Paused download ID {download_id}")

        self.update_table()

    def stop_download(self):
        selected = self.table.selection()
        if not selected:
            self.status_bar.config(text="No download selected")
            logging.debug("Stop attempted but no item selected")
            return

        for item in selected:
            download_id = int(self.table.item(item, "text"))
            download = self.downloads[download_id]
            if download.status in ["Downloading", "Paused"]:
                download.is_stopped = True
                download.is_paused = False
                download.status = "Stopped"
                self.db.update_download(download_id, download.progress, "Stopped", download.downloaded, download.is_paused)
                self.toggle_button.config(text="Start")
                self.status_bar.config(text=f"Stopped download: {os.path.basename(download.save_path)}")
                logging.debug(f"Stopped download ID {download_id}")

        self.update_table()

    def on_selection_change(self, event):
        self.update_toggle_button()

    def update_toggle_button(self):
        selected = self.table.selection()
        if not selected:
            self.toggle_button.config(text="Start")
            return

        for item in selected:
            download_id = int(self.table.item(item, "text"))
            download = self.downloads.get(download_id)
            if download:
                if download.status == "Downloading":
                    self.toggle_button.config(text="Pause")
                elif download.status == "Paused":
                    self.toggle_button.config(text="Resume")
                else:
                    self.toggle_button.config(text="Start")

    def delete_download(self):
        selected = self.table.selection()
        if not selected:
            self.status_bar.config(text="No download selected")
            logging.debug("Delete attempted but no item selected")
            return
        for item in selected:
            download_id = int(self.table.item(item, "text"))
            download = self.downloads[download_id]
            if messagebox.askyesno("Confirm Delete", f"Delete {os.path.basename(download.save_path)}? (File will be deleted)"):
                if download.status in ["Downloading", "Paused"]:
                    download.is_stopped = True
                    download.is_paused = False
                if os.path.exists(download.save_path):
                    os.remove(download.save_path)
                del self.downloads[download_id]
                self.db.delete_download(download_id)
                self.status_bar.config(text=f"Deleted download: {os.path.basename(download.save_path)}")
                logging.debug(f"Deleted download ID {download_id}")
        self.update_table()
        self.update_toggle_button()

    def update_download_progress(self, download_id: int, progress: float, speed: float, eta: float, status: str):
        if download_id in self.downloads:
            self.downloads[download_id].progress = progress
            self.downloads[download_id].speed = speed
            self.downloads[download_id].eta = eta
            self.downloads[download_id].status = status
            self.downloads[download_id].downloaded = int((progress / 100) * self.downloads[download_id].file_size) if self.downloads[download_id].file_size > 0 else self.downloads[download_id].downloaded
            self.db.update_download(download_id, progress, status, self.downloads[download_id].downloaded, self.downloads[download_id].is_paused)
            logging.debug(f"Updated progress for ID {download_id}: {progress}%, Status: {status}, Speed: {speed} Mb/s")
            self.update_toggle_button()

    def show_error(self, message: str):
        messagebox.showerror("Download Error", message)
        logging.error(f"Error displayed: {message}")
        self.status_bar.config(text="Error occurred, check log for details")
        self.update_toggle_button()

    def load_downloads(self):
        for download in self.db.get_downloads():
            download_id, url, save_path, file_size, status, progress, downloaded, is_paused = download
            self.downloads[download_id] = DownloadItem(url, save_path, file_size)
            self.downloads[download_id].status = status
            self.downloads[download_id].progress = progress
            self.downloads[download_id].downloaded = downloaded
            self.downloads[download_id].is_paused = bool(is_paused)
        self.update_table()

    def update_table(self):
        for item in self.table.get_children():
            download_id = int(self.table.item(item, "text"))
            if download_id not in self.downloads:
                self.table.delete(item)
        for download_id, download in self.downloads.items():
            exists = False
            for item in self.table.get_children():
                if int(self.table.item(item, "text")) == download_id:
                    exists = True
                    break
            filename = os.path.basename(download.save_path)
            size_mb = f"{download.file_size / (1024 * 1024):.2f}" if download.file_size else "Unknown"
            values = (
                filename,
                size_mb,
                f"{download.progress:.1f}",
                f"{download.speed:.2f}",
                f"{download.eta:.0f}",
                download.status
            )
            if exists:
                self.table.item(item, values=values)
            else:
                self.table.insert("", tk.END, text=str(download_id), values=values)

    def update_ui(self):
        self.update_table()
        self.update_toggle_button()
        self.after(200, self.update_ui)

def main():
    loop = asyncio.get_event_loop()
    window = AsyncDADAloaderWindow(loop)
    window.mainloop()

if __name__ == "__main__":
    main()