#########################
#BY RIFAT
#########################
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
import re

# --- Application Constants ---
APP_NAME = "DADAloader"
ARIA2C_DOWNLOAD_URL = "https://github.com/aria2/aria2/releases/download/release-1.37.0/aria2-1.37.0-win-64bit-build1.zip"
ARIA2C_EXPECTED_EXE_IN_ZIP = "aria2c.exe"

# --- Function to get user-specific app data directory ---
def get_app_data_dir():
    app_name_local = APP_NAME
    if sys.platform == "win32":
        path = os.path.join(os.environ.get('APPDATA', os.path.expanduser("~")), app_name_local)
    elif sys.platform == "darwin":
        path = os.path.join(os.path.expanduser("~"), "Library", "Application Support", app_name_local)
    else:
        path = os.path.join(os.path.expanduser("~"), ".config", app_name_local)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        logging.warning(f"Could not create app data directory {path}: {e}. Using script directory as fallback.")
        try:
            if getattr(sys, 'frozen', False):
                script_dir_fallback = os.path.dirname(sys.executable)
            else:
                script_dir_fallback = os.path.dirname(os.path.abspath(__file__))
            path = os.path.join(script_dir_fallback, app_name_local + "_data")
            os.makedirs(path, exist_ok=True)
        except Exception as fallback_e:
            logging.critical(f"Could not create fallback app data directory: {fallback_e}. Using current working directory.")
            path = os.getcwd()
    return path

APP_DATA_DIR = get_app_data_dir()

# --- Setup logging ---
LOG_FILE_PATH = os.path.join(APP_DATA_DIR, f'{APP_NAME.lower()}.log')
logging.basicConfig(
    filename=LOG_FILE_PATH,
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(module)s:%(lineno)d - %(message)s'
)
logging.info(f"--- Application Session Started --- App Name: {APP_NAME}")
logging.info(f"Logging to: {LOG_FILE_PATH}")
logging.info(f"Application Data Directory: {APP_DATA_DIR}")

class DownloadItem:
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
        self.process: Optional[asyncio.subprocess.Process] = None
        self.start_time: Optional[float] = None
        self.task: Optional[asyncio.Task] = None
        logging.debug(f"Created DownloadItem for {url} to {save_path}")

class DatabaseManager:
    def __init__(self, db_name: str = f"{APP_NAME.lower()}.db"):
        db_path = os.path.join(APP_DATA_DIR, db_name)
        self.conn = sqlite3.connect(db_path, timeout=10)
        self.create_tables()
        logging.info(f"Database initialized at: {db_path}")

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
                    is_paused INTEGER DEFAULT 0,
                    UNIQUE(url, save_path)
                )
            """)

    def add_download(self, download: DownloadItem) -> Optional[int]:
        try:
            with self.conn:
                cursor = self.conn.execute(
                    "INSERT INTO downloads (url, save_path, file_size, status, progress, downloaded, is_paused) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (download.url, download.save_path, download.file_size, download.status, download.progress, download.downloaded, int(download.is_paused))
                )
                return cursor.lastrowid
        except sqlite3.IntegrityError as e:
            logging.warning(f"Failed to add download (likely duplicate URL/save_path): {download.url} - {e}")
            return None
        except Exception as e:
            logging.error(f"Error adding download to DB: {e}", exc_info=True)
            return None

    def update_download(self, download_id: int, progress: float, status: str, downloaded: int, is_paused: bool, file_size: Optional[int] = None):
        with self.conn:
            if file_size is not None and file_size > 0:
                self.conn.execute(
                    "UPDATE downloads SET progress = ?, status = ?, downloaded = ?, is_paused = ?, file_size = ? WHERE id = ?",
                    (progress, status, downloaded, int(is_paused), file_size, download_id)
                )
            else:
                self.conn.execute(
                    "UPDATE downloads SET progress = ?, status = ?, downloaded = ?, is_paused = ? WHERE id = ?",
                    (progress, status, downloaded, int(is_paused), download_id)
                )

    def get_downloads(self) -> List[Tuple]:
        with self.conn:
            return self.conn.execute("SELECT id, url, save_path, file_size, status, progress, downloaded, is_paused FROM downloads ORDER BY id").fetchall()

    def delete_download(self, download_id: int):
        with self.conn:
            self.conn.execute("DELETE FROM downloads WHERE id = ?", (download_id,))

    def close(self):
        if self.conn:
            self.conn.close()
            logging.info("Database connection closed.")

class AsyncDownloader:
    def __init__(self, window):
        self.window = window
        self.aria2c_path = self._ensure_aria2c()
        logging.info(f"AsyncDownloader initialized. aria2c path: {self.aria2c_path}")

    def _show_deferred_message(self, msg_type: str, title: str, message_text: str):
        parent_window = self.window if self.window and self.window.winfo_exists() else None
        if parent_window and hasattr(parent_window, 'after'):
            try:
                parent_window.after(10, lambda: getattr(messagebox, msg_type)(title, message_text, parent=parent_window))
            except RuntimeError:
                logging.info(f"Deferred msg ({title}): {message_text} (Window not ready or closing). Displaying on console.")
                print(f"INFO ({title}): {message_text}")
        else:
            print(f"INFO ({title}): {message_text}")

    def _ensure_aria2c(self) -> Optional[str]:
        if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
            bundled_aria2c_path = os.path.join(sys._MEIPASS, ARIA2C_EXPECTED_EXE_IN_ZIP)
            if os.path.isfile(bundled_aria2c_path):
                logging.debug(f"Found bundled aria2c at: {bundled_aria2c_path}")
                return bundled_aria2c_path

        try:
            if getattr(sys, 'frozen', False):
                script_dir = os.path.dirname(sys.executable)
            else:
                script_dir = os.path.dirname(os.path.abspath(__file__))
        except NameError:
            script_dir = os.getcwd()

        local_aria2c_path = os.path.join(script_dir, "aria2c.exe")
        if os.path.isfile(local_aria2c_path):
            logging.debug(f"Found local aria2c (beside script/exe) at: {local_aria2c_path}")
            return local_aria2c_path

        path_aria2c = shutil.which("aria2c")
        if path_aria2c:
            logging.debug(f"Found aria2c in PATH: {path_aria2c}")
            return path_aria2c

        logging.info("aria2c not found. Attempting to download...")
        self._show_deferred_message("showinfo", "aria2c Not Found", "aria2c executable not found locally or in PATH. Attempting to download it now. This may take a moment.")

        download_target_dir = APP_DATA_DIR
        aria2c_downloaded_path = os.path.join(download_target_dir, ARIA2C_EXPECTED_EXE_IN_ZIP)

        if os.path.isfile(aria2c_downloaded_path):
            logging.info(f"Found previously downloaded aria2c at {aria2c_downloaded_path}")
            self._show_deferred_message("showinfo", "aria2c Found", f"Using previously downloaded aria2c from {download_target_dir}.")
            return aria2c_downloaded_path

        try:
            logging.info(f"Downloading aria2c from {ARIA2C_DOWNLOAD_URL}")
            response = requests.get(ARIA2C_DOWNLOAD_URL, stream=True, timeout=60)
            response.raise_for_status()

            zip_path = os.path.join(download_target_dir, "aria2c_download.zip")
            with open(zip_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            logging.info(f"aria2c ZIP downloaded to {zip_path}")

            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                extracted = False
                for member_info in zip_ref.infolist():
                    if member_info.filename.endswith(ARIA2C_EXPECTED_EXE_IN_ZIP):
                        temp_extract_path = os.path.join(download_target_dir, "temp_aria2_extract")
                        zip_ref.extract(member_info, temp_extract_path)
                        
                        extracted_file_full_path = os.path.join(temp_extract_path, member_info.filename)
                        
                        if os.path.exists(aria2c_downloaded_path):
                            os.remove(aria2c_downloaded_path)
                        shutil.move(extracted_file_full_path, aria2c_downloaded_path)
                        
                        shutil.rmtree(temp_extract_path)
                        
                        extracted = True
                        logging.info(f"Extracted '{member_info.filename}' to '{aria2c_downloaded_path}'")
                        break
                if not extracted:
                    raise FileNotFoundError(f"{ARIA2C_EXPECTED_EXE_IN_ZIP} not found in the downloaded zip: {zip_path}")

            os.remove(zip_path)
            logging.info(f"Successfully downloaded and extracted aria2c to: {aria2c_downloaded_path}")
            self._show_deferred_message("showinfo", "aria2c Download Successful", f"aria2c downloaded and set up successfully in {download_target_dir}.")
            return aria2c_downloaded_path

        except requests.exceptions.RequestException as e_req:
            logging.error(f"Network error downloading aria2c: {e_req}", exc_info=True)
            self._show_deferred_message("showerror", "aria2c Download Error", f"Failed to download aria2c (network issue): {e_req}")
            return None
        except Exception as e:
            logging.error(f"Failed to download/setup aria2c: {e}", exc_info=True)
            self._show_deferred_message("showerror", "aria2c Setup Critical Error", f"A critical error occurred while trying to obtain aria2c.\nError: {e}")
            return None

    def parse_size(self, size_str: str) -> int:
        size_str = size_str.strip().upper()
        if not size_str:
            return 0

        units = {
            'B': 1,
            'KIB': 1024, 'MIB': 1024**2, 'GIB': 1024**3, 'TIB': 1024**4,
            'KB': 1000, 'MB': 1000**2, 'GB': 1000**3, 'TB': 1000**4,
            'K': 1024, 'M': 1024**2, 'G': 1024**3, 'T': 1024**4,
        }
        units_strict_decimal = {'KB': 1000, 'MB': 1000**2, 'GB': 1000**3, 'TB': 1000**4}

        match = re.fullmatch(r'(\d+(?:\.\d+)?)\s*([KMGTB]I?B?)?', size_str)

        if not match:
            if re.fullmatch(r'\d+(?:\.\d+)?', size_str):
                try:
                    return int(float(size_str))
                except ValueError:
                    logging.warning(f"Could not parse size string as plain number: '{size_str}'")
                    return 0
            logging.warning(f"Could not parse size string format: '{size_str}'")
            return 0

        value_str, unit_str = match.groups()
        
        try:
            value = float(value_str)
        except ValueError:
            logging.warning(f"Could not parse numeric value from '{value_str}' in size string: '{size_str}'")
            return 0

        if unit_str:
            unit_str_upper = unit_str.upper()
            
            if unit_str_upper in units:
                multiplier = units[unit_str_upper]
            elif unit_str_upper in units_strict_decimal:
                multiplier = units_strict_decimal[unit_str_upper]
            else:
                base_unit = unit_str_upper[0]
                if base_unit in units:
                    multiplier = units[base_unit]
                else:
                    logging.warning(f"Unrecognized unit '{unit_str}' in size string: '{size_str}'. Interpreting '{value_str}' as bytes.")
                    return int(value)
            return int(value * multiplier)
        else:
            return int(value)

    def parse_eta(self, eta_str: str) -> int:
        eta_str = eta_str.replace("ETA:", "").replace("s]", "").strip()
        total_seconds = 0
        
        match = re.fullmatch(r'(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?', eta_str.lower())

        if match:
            hours, minutes, seconds = match.groups()
            if hours:
                total_seconds += int(hours) * 3600
            if minutes:
                total_seconds += int(minutes) * 60
            if seconds:
                total_seconds += int(seconds)
            return total_seconds
        else:
            if eta_str.isdigit():
                return int(eta_str)
            logging.warning(f"Could not parse ETA string: '{eta_str}'. Returning 0.")
            return 0

    def safe_decode(self, bytes_data: bytes, stream) -> str:
        """Safely decode bytes, falling back to 'utf-8' if stream is None or lacks encoding."""
        if stream and hasattr(stream, 'encoding') and stream.encoding:
            encoding = stream.encoding
        else:
            encoding = 'utf-8'
            logging.warning(f"Stream {stream} has no encoding attribute or is None. Using 'utf-8' for decoding.")
        return bytes_data.decode(encoding, errors='replace')

    async def download(self, download_id: int, download_item: DownloadItem):
        if not self.aria2c_path or not os.path.exists(self.aria2c_path):
            error_msg = f"aria2c path is invalid ('{self.aria2c_path}') or file does not exist."
            logging.critical(f"{error_msg} Cannot start download ID {download_id}.")
            download_item.status = "Error: Misconfig"
            self.window.schedule_update_download_progress(download_id, download_item.progress, 0.0, 0, download_item.status, download_item.file_size)
            if self.window and self.window.winfo_exists():
                self.window.show_error("aria2c executable is misconfigured or missing. Downloads cannot start. Please check logs or restart the application.")
            return

        logging.info(f"--- Download Task Started for ID {download_id} ---")
        logging.info(f"  URL: {download_item.url}")
        logging.info(f"  Save Path: '{download_item.save_path}'")
        logging.info(f"  State BEFORE launch: status='{download_item.status}', prog={download_item.progress:.1f}%, dl={download_item.downloaded}, size={download_item.file_size}")

        control_file_path = download_item.save_path + ".aria2"
        logging.debug(f"  Control file path: '{control_file_path}'")
        
        is_resuming = (download_item.downloaded > 0 or download_item.progress > 0 or os.path.exists(control_file_path)) and \
                      download_item.status not in ["Pending", "Error (RC13)"]
        
        current_status_for_ui = "Resuming..." if is_resuming and download_item.status == "Paused" else "Connecting..."
        if download_item.status == "Error (RC13)": current_status_for_ui = "Retrying (fresh)..."
        
        self.window.schedule_update_download_progress(download_id, download_item.progress, download_item.speed, download_item.eta, current_status_for_ui, download_item.file_size)

        try:
            download_dir = os.path.dirname(download_item.save_path)
            if not os.path.isdir(download_dir):
                try:
                    os.makedirs(download_dir, exist_ok=True)
                    logging.info(f"  Created download directory: '{download_dir}'")
                except OSError as e:
                    logging.error(f"Download directory '{download_dir}' for ID {download_id} could not be created: {e}. Aborting.")
                    download_item.status = "Error: Bad Dir"
                    self.window.schedule_update_download_progress(download_id, download_item.progress, 0.0, 0, download_item.status, download_item.file_size)
                    return

            logging.info(f"  Using CWD for aria2c: '{download_dir}'")
            base_filename = os.path.basename(download_item.save_path)

            common_args = [
                "--dir", download_dir,
                "--out", base_filename,
                "--summary-interval=1",
                "--console-log-level=warn",
                "--show-console-readout=false",
                "--allow-overwrite=false",
                "--auto-file-renaming=false",
            ]
            
            if is_resuming:
                cmd_args = ["--continue=true"] + common_args + [download_item.url]
            else:
                cmd_args = ["--max-connection-per-server=8", "--split=8"] + common_args + [download_item.url]
            
            full_cmd = [self.aria2c_path] + cmd_args
            logging.info(f"  Final aria2c command for ID {download_id}: {' '.join(full_cmd)}")
            
            download_item.start_time = time.time()
            creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

            while not download_item.is_stopped:
                logging.info(f"  Outer Download Loop Iteration for ID {download_id}. Current Status: {download_item.status}")
                process = None
                try:
                    logging.debug(f"    ID {download_id}: About to call asyncio.create_subprocess_exec...")
                    process = await asyncio.create_subprocess_exec(
                        *full_cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        creationflags=creation_flags,
                        cwd=download_dir
                    )
                    download_item.process = process
                    logging.info(f"    ID {download_id}: aria2c process started. PID {process.pid if process else 'N/A'}.")
                    if not process or process.pid is None:
                        logging.error(f"    ID {download_id}: Failed to create aria2c process object or PID is None.")
                        download_item.status = "Error: Launch Failed"
                        self.window.schedule_update_download_progress(download_id, download_item.progress, 0.0, 0, download_item.status, download_item.file_size)
                        return

                except Exception as e_proc_create:
                    logging.error(f"    ID {download_id}: Exception during create_subprocess_exec: {e_proc_create}", exc_info=True)
                    download_item.status = "Error: Launch Exception"
                    self.window.schedule_update_download_progress(download_id, download_item.progress, 0.0, 0, download_item.status, download_item.file_size)
                    return

                async def log_stream(stream_name, stream_obj, dl_id, proc_pid):
                    logging.debug(f"      log_stream for {stream_name} (ID {dl_id}, PID {proc_pid}) started.")
                    try:
                        async for line_bytes in stream_obj:
                            line_text = self.safe_decode(line_bytes, sys.stderr).strip()
                            if line_text:
                                log_level = logging.WARNING if stream_name == "STDERR" else logging.DEBUG
                                logging.log(log_level, f"      aria2c {stream_name} (ID {dl_id}, PID {proc_pid}): {line_text}")
                    except Exception as e_stream:
                        logging.error(f"      Exception in log_stream for {stream_name} (ID {dl_id}, PID {proc_pid}): {e_stream}", exc_info=True)
                    logging.debug(f"      log_stream for {stream_name} (ID {dl_id}, PID {proc_pid}) finished.")

                stderr_logger_task = asyncio.create_task(log_stream("STDERR", process.stderr, download_id, process.pid))
                
                initial_progress_received_this_session = False
                stdout_line_count = 0
                logging.debug(f"    ID {download_id}, PID {process.pid}: Entering stdout processing loop...")

                first_line_processed = False
                try:
                    line_bytes = await asyncio.wait_for(process.stdout.readline(), timeout=10.0)
                    if line_bytes:
                        stdout_line_count += 1
                        line = self.safe_decode(line_bytes, sys.stdout).strip()
                        logging.debug(f"  aria2c STDOUT (ID {download_id}, PID {process.pid}, FirstLine): {line}")
                        if "[#" in line and ("ETA:" in line or "CN:" in line or "DL:" in line):
                            self._parse_and_update_aria2c_output(download_id, download_item, line, is_resuming, initial_progress_received_this_session)
                            initial_progress_received_this_session = True
                        first_line_processed = True
                    else:
                        logging.warning(f"      ID {download_id}, PID {process.pid}: FIRST stdout.readline() returned NO bytes (EOF).")
                except asyncio.TimeoutError:
                    logging.warning(f"      ID {download_id}, PID {process.pid}: TIMEOUT on FIRST stdout.readline(). aria2c might be stuck or slow to start.")
                except Exception as e_first_line:
                    logging.error(f"      ID {download_id}, PID {process.pid}: Exception on FIRST stdout.readline(): {e_first_line}", exc_info=True)

                while process.returncode is None:
                    try:
                        line_bytes = await asyncio.wait_for(process.stdout.readline(), timeout=1.5)
                    except asyncio.TimeoutError:
                        if download_item.is_stopped or download_item.is_paused:
                            break
                        if process.returncode is not None:
                            break
                        continue

                    if not line_bytes:
                        logging.debug(f"    ID {download_id}, PID {process.pid}: stdout EOF reached.")
                        break

                    stdout_line_count += 1
                    line = self.safe_decode(line_bytes, sys.stdout).strip()
                    if line:
                        logging.debug(f"  aria2c STDOUT (ID {download_id}, PID {process.pid}, Line {stdout_line_count}): {line}")

                    if download_item.is_stopped:
                        logging.info(f"    ID {download_id}: STOP flag detected. Terminating process.")
                        if process.returncode is None: process.terminate()
                        download_item.status = "Stopping..."
                        break
                    
                    if download_item.is_paused:
                        logging.info(f"    ID {download_id}: PAUSE flag detected. Terminating process for pause.")
                        if process.returncode is None: process.terminate()
                        download_item.status = "Pausing..."
                        break

                    if "[#" in line and ("ETA:" in line or "CN:" in line or "DL:" in line):
                        self._parse_and_update_aria2c_output(download_id, download_item, line, is_resuming, initial_progress_received_this_session)
                        initial_progress_received_this_session = True
                
                rc = await process.wait()
                logging.info(f"  aria2c process (PID {getattr(process,'pid','N/A')}) for ID {download_id} exited with RC: {rc}.")
                download_item.process = None

                if not stderr_logger_task.done():
                    stderr_logger_task.cancel()
                    try:
                        await asyncio.wait_for(stderr_logger_task, timeout=1.0)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        logging.debug(f"    ID {download_id}: stderr_logger_task cancellation/timeout.")
                        pass
                
                current_db_file_size = download_item.file_size

                if download_item.status == "Stopping...":
                    download_item.status = "Stopped"
                elif download_item.status == "Pausing...":
                    download_item.status = "Paused"
                
                if download_item.is_stopped:
                    self.window.schedule_update_download_progress(download_id, download_item.progress, 0.0, 0, download_item.status, current_db_file_size)
                    break

                if download_item.is_paused:
                    self.window.schedule_update_download_progress(download_id, download_item.progress, 0.0, 0, download_item.status, current_db_file_size)
                    while download_item.is_paused and not download_item.is_stopped:
                        await asyncio.sleep(0.5)
                    
                    if download_item.is_stopped:
                        download_item.status = "Stopped"
                        self.window.schedule_update_download_progress(download_id, download_item.progress, 0.0, 0, download_item.status, current_db_file_size)
                        break
                    
                    logging.info(f"  ID {download_id}: Resuming from PAUSED state. Continuing outer loop.")
                    is_resuming = True
                    download_item.status = "Pending Resume"
                    self.window.schedule_update_download_progress(download_id, download_item.progress, 0.0, 0, "Resuming...", current_db_file_size)
                    continue

                if rc == 0:
                    if os.path.exists(download_item.save_path) and download_item.file_size > 0 and \
                       os.path.getsize(download_item.save_path) >= download_item.file_size:
                        download_item.progress = 100.0
                        download_item.downloaded = download_item.file_size
                    
                    if download_item.progress >= 99.9:
                        download_item.status = "Completed"
                        download_item.progress = 100.0
                        if download_item.file_size > 0: download_item.downloaded = download_item.file_size
                        self.window.schedule_update_download_progress(download_id, 100.0, 0.0, 0, "Completed", download_item.file_size)
                        break
                    else:
                        logging.warning(f"  aria2c for ID {download_id} exited with RC 0, but progress is {download_item.progress:.1f}%. Retrying.")
                        await asyncio.sleep(2)
                        is_resuming = True
                        self.window.schedule_update_download_progress(download_id, download_item.progress, download_item.speed, download_item.eta, "Retrying...", current_db_file_size)
                        continue
                else:
                    logging.error(f"  Download ID {download_id} failed: aria2c exited with RC {rc}.")
                    if rc == 3:
                        logging.warning(f"    aria2c RC {rc}: File possibly already exists or other resource issue for ID {download_id}.")
                        if os.path.exists(download_item.save_path) and download_item.file_size > 0 and \
                           os.path.getsize(download_item.save_path) >= download_item.file_size:
                            download_item.status = "Completed"; download_item.progress = 100.0
                            self.window.schedule_update_download_progress(download_id, 100.0, 0.0, 0, "Completed", download_item.file_size)
                        else:
                            download_item.status = f"Error (RC{rc})"
                            self.window.schedule_update_download_progress(download_id, download_item.progress, 0.0, 0, download_item.status, current_db_file_size)
                    elif rc == 13:
                        logging.error(f"    aria2c RC 13: Session file (.aria2) not found or invalid. Resume failed for ID {download_id}.")
                        download_item.status = "Error (RC13)"
                        download_item.progress = 0
                        download_item.downloaded = 0
                        self.window.schedule_update_download_progress(download_id, download_item.progress, 0.0, 0, download_item.status, current_db_file_size)
                    else:
                        download_item.status = f"Error (RC{rc})"
                        self.window.schedule_update_download_progress(download_id, download_item.progress, 0.0, 0, download_item.status, current_db_file_size)
                    break

            logging.info(f"  Exited outer download loop for ID {download_id}. Final status before task end: {download_item.status}")

        except asyncio.CancelledError:
            logging.info(f"Download task for ID {download_id} was CANCELLED by application.")
            proc = download_item.process
            if proc and proc.returncode is None:
                logging.info(f"  Terminating aria2c process {proc.pid} due to cancellation.")
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    logging.warning(f"  aria2c process {proc.pid} did not terminate gracefully, killing.")
                    proc.kill()
                except Exception as e_term:
                    logging.error(f"  Error during process termination on cancel: {e_term}")
            if download_item.status not in ["Completed", "Error", "Stopped", "Paused"]:
                download_item.status = "Cancelled"
            self.window.schedule_update_download_progress(download_id, download_item.progress, 0.0, 0, download_item.status, download_item.file_size)

        except Exception as e:
            logging.critical(f"CRITICAL UNHANDLED error in download task for ID {download_id} ('{download_item.url}'): {e}", exc_info=True)
            download_item.status = "Error (Exception)"
            if self.window and self.window.winfo_exists():
                self.window.show_error(f"A critical error occurred during the download of '{os.path.basename(download_item.save_path)}': {e}\nPlease check logs.")
            self.window.schedule_update_download_progress(download_id, download_item.progress, 0.0, 0, download_item.status, download_item.file_size)
        
        finally:
            logging.info(f"--- Download Task Finished for ID {download_id}. Final Status: {download_item.status} ---")
            if download_item.process and download_item.process.returncode is None:
                logging.warning(f"  Process {download_item.process.pid} for ID {download_id} still running in finally block. Terminating.")
                download_item.process.terminate()
                try:
                    await asyncio.wait_for(download_item.process.wait(), timeout=1.0)
                except (asyncio.TimeoutError, Exception):
                    if download_item.process.returncode is None:
                        download_item.process.kill()
            download_item.process = None
            download_item.task = None

    def _parse_and_update_aria2c_output(self, download_id: int, download_item: DownloadItem, line: str,
                                        is_resuming_session: bool, initial_progress_received_this_session: bool):
        try:
            parts = line.split()
            
            parsed_downloaded_this_line = -1
            parsed_total_this_line = -1
            new_progress_percent = download_item.progress
            new_speed_bytes_sec = download_item.speed * (1024*1024) / 8
            new_eta_sec = download_item.eta

            for part in parts:
                if "/" in part and "(" in part and "%" in part:
                    try:
                        dl_str, total_and_prog_str = part.split("/", 1)
                        total_str, prog_perc_str = total_and_prog_str.split("(", 1)
                        
                        parsed_downloaded_this_line = self.parse_size(dl_str)
                        parsed_total_this_line = self.parse_size(total_str)
                        new_progress_percent = float(prog_perc_str.rstrip("%)"))

                        if parsed_total_this_line > 0:
                            if download_item.file_size <= 0 or download_item.file_size != parsed_total_this_line:
                                logging.info(f"    ID {download_id}: File size updated by aria2c: {download_item.file_size} -> {parsed_total_this_line}")
                                download_item.file_size = parsed_total_this_line
                            new_progress_percent = (parsed_downloaded_this_line / parsed_total_this_line) * 100 if parsed_total_this_line > 0 else new_progress_percent
                        
                        if is_resuming_session and not initial_progress_received_this_session and \
                           download_item.downloaded > (512 * 1024) and \
                           parsed_downloaded_this_line < (256 * 1024) and \
                           parsed_downloaded_this_line < download_item.downloaded:
                            logging.warning(f"    ID {download_id}: Potential progress reset detected by aria2c. "
                                            f"Old downloaded: {download_item.downloaded}, New: {parsed_downloaded_this_line}. Line: '{line}'")
                        
                        download_item.downloaded = parsed_downloaded_this_line
                        break
                    except ValueError:
                        logging.warning(f"    ID {download_id}: Could not parse progress percentage from '{part}' in line '{line}'")
                    except Exception as e_prog_parse:
                        logging.warning(f"    ID {download_id}: Error parsing progress part '{part}': {e_prog_parse}")

            for part in parts:
                if part.startswith("DL:"):
                    speed_str = part.replace("DL:", "")
                    new_speed_bytes_sec = self.parse_size(speed_str)
                    break
            
            for part in parts:
                if part.startswith("ETA:"):
                    eta_str = part
                    new_eta_sec = self.parse_eta(eta_str)
                    break
            
            download_item.progress = max(0.0, min(100.0, new_progress_percent))
            download_item.speed = (new_speed_bytes_sec * 8) / (1024 * 1024)
            download_item.eta = new_eta_sec

            self.window.schedule_update_download_progress(
                download_id,
                download_item.progress,
                download_item.speed,
                download_item.eta,
                "Downloading",
                download_item.file_size
            )

        except Exception as e_parse:
            logging.error(f"    ID {download_id}: Error parsing aria2c output line '{line}': {e_parse}", exc_info=True)

class FileInfoDialog(tk.Toplevel):
    def __init__(self, parent, download_id: int, initial_download_item: DownloadItem):
        super().__init__(parent)
        self.title("File Information")
        self.download_id = download_id
        self.parent_window = parent
        
        self.transient(parent)
        self.grab_set()

        self._init_ui(initial_download_item)
        self._update_info_job_id = None
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._schedule_info_update()
        self._center_dialog(parent)

    def _init_ui(self, item: DownloadItem):
        frame = ttk.Frame(self, padding="10")
        frame.pack(padx=10, pady=10, fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        row_idx = 0
        ttk.Label(frame, text="Filename:").grid(row=row_idx, column=0, sticky="w", padx=5, pady=2)
        self.filename_label = ttk.Label(frame, text=os.path.basename(item.save_path), wraplength=350, anchor="w")
        self.filename_label.grid(row=row_idx, column=1, sticky="ew", padx=5, pady=2)
        row_idx += 1

        ttk.Label(frame, text="URL:").grid(row=row_idx, column=0, sticky="w", padx=5, pady=2)
        self.url_label = ttk.Label(frame, text=item.url, wraplength=350, anchor="w")
        self.url_label.grid(row=row_idx, column=1, sticky="ew", padx=5, pady=2)
        row_idx += 1
        
        ttk.Label(frame, text="Save Path:").grid(row=row_idx, column=0, sticky="w", padx=5, pady=2)
        self.save_path_label = ttk.Label(frame, text=item.save_path, wraplength=350, anchor="w")
        self.save_path_label.grid(row=row_idx, column=1, sticky="ew", padx=5, pady=2)
        row_idx += 1

        size_mb_str = f"{item.file_size / (1024*1024):.2f} MB" if item.file_size else "Unknown"
        ttk.Label(frame, text="Total Size:").grid(row=row_idx, column=0, sticky="w", padx=5, pady=2)
        self.size_label = ttk.Label(frame, text=size_mb_str, anchor="w")
        self.size_label.grid(row=row_idx, column=1, sticky="ew", padx=5, pady=2)
        row_idx += 1

        progress_frame = ttk.Frame(frame)
        progress_frame.grid(row=row_idx, column=1, sticky="ew", padx=5, pady=2)
        progress_frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text="Progress:").grid(row=row_idx, column=0, sticky="w", padx=5, pady=2)
        self.progress_bar = ttk.Progressbar(progress_frame, length=300, mode='determinate', value=item.progress)
        self.progress_bar.grid(row=0, column=0, sticky="ew")
        self.progress_label_text = ttk.Label(progress_frame, text=f"{item.progress:.1f}%", anchor="w")
        self.progress_label_text.grid(row=0, column=1, sticky="w", padx=5)
        row_idx += 1

        ttk.Label(frame, text="Downloaded:").grid(row=row_idx, column=0, sticky="w", padx=5, pady=2)
        self.downloaded_label = ttk.Label(frame, text=f"{item.downloaded / (1024*1024):.2f} MB", anchor="w")
        self.downloaded_label.grid(row=row_idx, column=1, sticky="ew", padx=5, pady=2)
        row_idx += 1
        
        left_mb = (item.file_size - item.downloaded) / (1024*1024) if item.file_size > 0 else 0.0
        ttk.Label(frame, text="Left:").grid(row=row_idx, column=0, sticky="w", padx=5, pady=2)
        self.left_label = ttk.Label(frame, text=f"{left_mb:.2f} MB" if item.file_size > 0 else "Unknown", anchor="w")
        self.left_label.grid(row=row_idx, column=1, sticky="ew", padx=5, pady=2)
        row_idx += 1

        ttk.Label(frame, text="Speed:").grid(row=row_idx, column=0, sticky="w", padx=5, pady=2)
        self.speed_label = ttk.Label(frame, text=f"{item.speed:.2f} Mbps", anchor="w")
        self.speed_label.grid(row=row_idx, column=1, sticky="ew", padx=5, pady=2)
        row_idx += 1
        
        ttk.Label(frame, text="Time Left (ETA):").grid(row=row_idx, column=0, sticky="w", padx=5, pady=2)
        self.time_left_label = ttk.Label(frame, text=f"{int(item.eta)}s", anchor="w")
        self.time_left_label.grid(row=row_idx, column=1, sticky="ew", padx=5, pady=2)
        row_idx += 1

        ttk.Label(frame, text="Status:").grid(row=row_idx, column=0, sticky="w", padx=5, pady=2)
        self.status_label = ttk.Label(frame, text=item.status, anchor="w")
        self.status_label.grid(row=row_idx, column=1, sticky="ew", padx=5, pady=2)
        row_idx += 1
        
        ttk.Button(frame, text="Close", command=self._on_close).grid(row=row_idx, column=0, columnspan=2, pady=10)

    def _center_dialog(self, parent_window):
        self.update_idletasks()
        parent_x = parent_window.winfo_rootx()
        parent_y = parent_window.winfo_rooty()
        parent_width = parent_window.winfo_width()
        parent_height = parent_window.winfo_height()
        
        dialog_width = self.winfo_reqwidth()
        dialog_height = self.winfo_reqheight()
        
        x_pos = parent_x + (parent_width // 2) - (dialog_width // 2)
        y_pos = parent_y + (parent_height // 2) - (dialog_height // 2)
        
        self.geometry(f"{dialog_width}x{dialog_height}+{x_pos}+{y_pos}")

    def _schedule_info_update(self):
        if not self.winfo_exists() or not self.parent_window.winfo_exists():
            self._cancel_info_update()
            return

        live_item = self.parent_window.downloads.get(self.download_id)
        if not live_item:
            self._on_close()
            return
        
        self.filename_label.config(text=os.path.basename(live_item.save_path))
        self.url_label.config(text=live_item.url)
        self.save_path_label.config(text=live_item.save_path)
        
        size_str = f"{live_item.file_size / (1024*1024):.2f} MB" if live_item.file_size > 0 else "Unknown"
        self.size_label.config(text=size_str)
        
        self.progress_bar['value'] = live_item.progress
        self.progress_label_text.config(text=f"{live_item.progress:.1f}%")
        
        dl_mb = live_item.downloaded / (1024*1024)
        left_mb = (live_item.file_size - live_item.downloaded) / (1024*1024) if live_item.file_size > 0 else 0.0
        self.downloaded_label.config(text=f"{dl_mb:.2f} MB")
        self.left_label.config(text=f"{left_mb:.2f} MB" if live_item.file_size > 0 else "Unknown")
        
        self.speed_label.config(text=f"{live_item.speed:.2f} Mbps")
        self.time_left_label.config(text=f"{int(live_item.eta)}s")
        self.status_label.config(text=live_item.status)
        
        self._update_info_job_id = self.after(500, self._schedule_info_update)

    def _cancel_info_update(self):
        if self._update_info_job_id:
            self.after_cancel(self._update_info_job_id)
            self._update_info_job_id = None

    def _on_close(self):
        self._cancel_info_update()
        self.grab_release()
        if self.winfo_exists():
            self.destroy()
        if hasattr(self.parent_window, 'on_file_info_dialog_close'):
            self.parent_window.on_file_info_dialog_close(self.download_id)

class AddDownloadDialog(tk.Toplevel):
    def __init__(self, parent, initial_url: str = ""):
        super().__init__(parent)
        self.title("Add New Download")
        self.parent = parent
        self.result: Optional[Tuple[str, str]] = None

        self.default_download_dir = os.path.join(os.path.expanduser("~"), "Downloads", APP_NAME)
        try:
            os.makedirs(self.default_download_dir, exist_ok=True)
        except OSError as e:
            logging.warning(f"Could not create default download directory '{self.default_download_dir}': {e}. Using current working directory as fallback.")
            self.default_download_dir = os.getcwd()

        self._init_ui(initial_url)
        
        self.transient(parent)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self._center_dialog(parent)
        self.url_entry.focus_set()
        self.wait_window(self)

    def _init_ui(self, initial_url: str):
        frame = ttk.Frame(self, padding="10")
        frame.pack(padx=10, pady=10, fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="URL:").grid(row=0, column=0, sticky="e", padx=5, pady=5)
        self.url_entry = ttk.Entry(frame, width=60)
        self.url_entry.insert(0, initial_url)
        self.url_entry.grid(row=0, column=1, padx=5, pady=5, columnspan=2, sticky="ew")
        
        default_filename = self._get_unique_filename_from_url(initial_url) if initial_url and validators.url(initial_url) else f"download_{int(time.time())}"
        default_save_path = os.path.join(self.default_download_dir, default_filename)

        ttk.Label(frame, text="Save Path:").grid(row=1, column=0, sticky="e", padx=5, pady=5)
        self.path_entry = ttk.Entry(frame, width=60)
        self.path_entry.insert(0, default_save_path)
        self.path_entry.grid(row=1, column=1, padx=5, pady=5, sticky="ew")
        
        browse_button = ttk.Button(frame, text="Browse...", command=self._browse_save_path)
        browse_button.grid(row=1, column=2, padx=5, pady=5)

        self.url_entry.bind("<FocusOut>", self._update_filename_from_url_event)
        self.url_entry.bind("<Return>", lambda event: self.path_entry.focus_set())

        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=2, column=0, columnspan=3, pady=(10,0), sticky="e")
        
        ok_button = ttk.Button(btn_frame, text="OK", command=self._on_ok, style="Accent.TButton")
        ok_button.pack(side=tk.LEFT, padx=5)
        cancel_button = ttk.Button(btn_frame, text="Cancel", command=self._on_cancel)
        cancel_button.pack(side=tk.LEFT, padx=5)

        self.bind("<Return>", lambda event: self._on_ok())
        self.bind("<Escape>", lambda event: self._on_cancel())

    def _get_filename_from_url(self, url: str) -> str:
        if not url or not validators.url(url):
            return f"download_{int(time.time())}"
        try:
            path = urlparse(url).path
            filename = os.path.basename(path)
            sanitized_filename = "".join(c for c in filename if c.isalnum() or c in ('.', '_', '-')).strip()
            return sanitized_filename if sanitized_filename else f"download_{int(time.time())}"
        except Exception as e:
            logging.warning(f"Could not extract filename from URL '{url}': {e}")
            return f"download_{int(time.time())}"

    def _get_unique_filename_from_url(self, url: str, directory: Optional[str] = None) -> str:
        target_dir = directory or self.default_download_dir
        base_filename = self._get_filename_from_url(url)
        name, ext = os.path.splitext(base_filename)
        name = name or "download"
        
        counter = 1
        unique_filename = base_filename
        while os.path.exists(os.path.join(target_dir, unique_filename)):
            unique_filename = f"{name}_{counter}{ext}"
            counter += 1
        return unique_filename

    def _update_filename_from_url_event(self, event=None):
        current_url = self.url_entry.get().strip()
        if validators.url(current_url):
            current_save_path = self.path_entry.get().strip()
            current_save_dir = os.path.dirname(current_save_path)
            
            is_default_location = (current_save_dir == self.default_download_dir and
                                   (os.path.basename(current_save_path).startswith("download_") or
                                    not os.path.exists(current_save_path)))

            if is_default_location:
                new_filename = self._get_unique_filename_from_url(current_url, self.default_download_dir)
                self.path_entry.delete(0, tk.END)
                self.path_entry.insert(0, os.path.join(self.default_download_dir, new_filename))

    def _browse_save_path(self):
        initial_dir = os.path.dirname(self.path_entry.get()) or self.default_download_dir
        initial_file = os.path.basename(self.path_entry.get())
        
        file_path = filedialog.asksaveasfilename(
            initialdir=initial_dir,
            initialfile=initial_file,
            title="Save As",
            defaultextension=".*",
            parent=self
        )
        if file_path:
            self.path_entry.delete(0, tk.END)
            self.path_entry.insert(0, file_path)

    def _on_ok(self, event=None):
        url = self.url_entry.get().strip()
        save_path = self.path_entry.get().strip()

        if not validators.url(url):
            messagebox.showerror("Invalid URL", "The entered URL is not valid.", parent=self)
            self.url_entry.focus_set()
            return
        
        if not save_path:
            messagebox.showerror("Invalid Path", "The save path cannot be empty.", parent=self)
            self.path_entry.focus_set()
            return

        save_dir = os.path.dirname(save_path)
        if not os.path.isdir(save_dir):
            try:
                os.makedirs(save_dir, exist_ok=True)
            except OSError as e:
                messagebox.showerror("Path Error", f"Cannot create directory: {save_dir}\nError: {e}", parent=self)
                return
        
        if not os.access(save_dir, os.W_OK):
            messagebox.showerror("Permission Error", f"No write access to the directory: {save_dir}", parent=self)
            return
        
        if os.path.exists(save_path) and not save_path.endswith(".aria2"):
            if not messagebox.askyesno("File Exists",
                                       f"The file '{os.path.basename(save_path)}' already exists.\n\n"
                                       "Do you want to overwrite or attempt to resume?",
                                       parent=self):
                return

        self.result = (url, save_path)
        self.grab_release()
        self.destroy()

    def _on_cancel(self, event=None):
        self.result = None
        self.grab_release()
        self.destroy()

    def _center_dialog(self, parent_window):
        self.update_idletasks()
        parent_x = parent_window.winfo_rootx()
        parent_y = parent_window.winfo_rooty()
        parent_width = parent_window.winfo_width()
        parent_height = parent_window.winfo_height()
        
        dialog_width = self.winfo_reqwidth()
        dialog_height = self.winfo_reqheight()
        
        x_pos = parent_x + (parent_width // 2) - (dialog_width // 2)
        y_pos = parent_y + (parent_height // 2) - (dialog_height // 2)
        
        self.geometry(f"+{x_pos}+{y_pos}")

class AsyncDADAloaderWindow(tk.Tk):
    def __init__(self, loop: asyncio.AbstractEventLoop):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("950x650")
        self.loop = loop
        self._is_destroying_startup = False

        self.db = DatabaseManager()
        self.downloads: dict[int, DownloadItem] = {}
        
        try:
            self.downloader = AsyncDownloader(self)
            if not self.downloader.aria2c_path:
                logging.critical("Downloader initialization failed: aria2c_path is None.")
                self.show_startup_error(
                    "aria2c (the downloader component) could not be initialized. "
                    "The application may not be able to download files.\n"
                    "Please check logs for details or try restarting."
                )
        except Exception as e:
            logging.critical(f"Failed to initialize AsyncDownloader: {e}", exc_info=True)
            self.show_startup_error(
                f"A critical error occurred during downloader initialization: {e}\n"
                "The application will close. Please check logs."
            )
            return

        self.last_clipboard_content = ""
        self.active_file_info_dialogs: dict[int, FileInfoDialog] = {}

        self._init_ui()
        self.load_downloads_from_db()

        self.after(100, self._process_asyncio_tasks)
        self.after(1000, self._check_clipboard_for_urls)
        self.after(1000, self._update_ui_periodically)

        self.protocol("WM_DELETE_WINDOW", self._on_closing_attempt)

    def show_startup_error(self, message: str):
        self._is_destroying_startup = True
        logging.error(f"STARTUP ERROR: {message}")
        try:
            if self.winfo_exists():
                messagebox.showerror("Startup Critical Error", message, parent=self if self.winfo_exists() else None)
            else:
                messagebox.showerror("Startup Critical Error", message)
        except tk.TclError:
            print(f"STARTUP CRITICAL ERROR (GUI not fully ready): {message}")
        
        if hasattr(self, 'after') and self.winfo_exists():
            self.after(100, self.destroy)
        else:
            sys.exit(1)

    def _init_ui(self):
        style = ttk.Style(self)
        available_themes = style.theme_names()
        preferred_themes = ['clam', 'alt', 'default']
        chosen_theme = style.theme_use()
        for theme_name in preferred_themes:
            if theme_name in available_themes:
                try:
                    style.theme_use(theme_name)
                    chosen_theme = theme_name
                    logging.debug(f"Using theme: {chosen_theme}")
                    break
                except tk.TclError:
                    logging.warning(f"Failed to apply theme '{theme_name}'.")
        
        style.configure("Accent.TButton", font=("Segoe UI", 9, "bold"))

        self.main_frame = ttk.Frame(self, padding="5")
        self.main_frame.pack(fill=tk.BOTH, expand=True)

        self.table_frame = ttk.Frame(self.main_frame)
        self.table_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        self.table_scroll_y = ttk.Scrollbar(self.table_frame, orient=tk.VERTICAL)
        self.table_scroll_y.pack(side=tk.RIGHT, fill=tk.Y)
        self.table_scroll_x = ttk.Scrollbar(self.table_frame, orient=tk.HORIZONTAL)
        self.table_scroll_x.pack(side=tk.BOTTOM, fill=tk.X)

        cols = ("ID", "Filename", "Size (MB)", "Progress", "Speed (Mbps)", "ETA (s)", "Status", "Save Path")
        self.table = ttk.Treeview(
            self.table_frame,
            columns=cols,
            show="headings",
            selectmode="browse",
            yscrollcommand=self.table_scroll_y.set,
            xscrollcommand=self.table_scroll_x.set
        )
        
        col_widths = {"ID": 40, "Filename": 280, "Size (MB)": 80, "Progress": 120,
                      "Speed (Mbps)": 100, "ETA (s)": 70, "Status": 130, "Save Path": 250}
        col_anchors = {"ID": "c", "Filename": "w", "Size (MB)": "e", "Progress": "e",
                       "Speed (Mbps)": "e", "ETA (s)": "e", "Status": "w", "Save Path": "w"}
        
        for col_name in cols:
            self.table.heading(col_name, text=col_name)
            self.table.column(col_name, width=col_widths.get(col_name, 100),
                              anchor=col_anchors.get(col_name, "w"),
                              stretch=(col_name in ["Filename", "Save Path"]))

        self.table.pack(fill=tk.BOTH, expand=True)
        self.table_scroll_y.config(command=self.table.yview)
        self.table_scroll_x.config(command=self.table.xview)

        self.table.bind("<Double-1>", self._on_table_double_click_show_info)
        self.table.bind("<<TreeviewSelect>>", self._on_table_selection_change)
        self.table.bind("<Button-3>", self._show_context_menu)

        button_frame = ttk.Frame(self.main_frame, padding="5")
        button_frame.pack(fill=tk.X, padx=5, pady=(0,5))

        ttk.Button(button_frame, text="➕ Add Download", command=lambda: self.show_add_download_dialog(""), style="Accent.TButton").pack(side=tk.LEFT, padx=3)
        self.toggle_button = ttk.Button(button_frame, text="▶️ Start", command=self._toggle_selected_download, width=12)
        self.toggle_button.pack(side=tk.LEFT, padx=3)
        self.stop_button = ttk.Button(button_frame, text="⏹️ Stop", command=self._stop_selected_download_action, width=10)
        self.stop_button.pack(side=tk.LEFT, padx=3)
        ttk.Button(button_frame, text="❌ Delete", command=self._delete_selected_download_with_prompt).pack(side=tk.LEFT, padx=3)

        self.status_bar_frame = ttk.Frame(self, relief="sunken", padding=2)
        self.status_bar_frame.pack(fill=tk.X, side=tk.BOTTOM)
        self.status_bar_label = ttk.Label(self.status_bar_frame, text="Ready", anchor="w")
        self.status_bar_label.pack(fill=tk.X, padx=5)

        self._update_action_buttons_state()

    def _show_context_menu(self, event):
        selected_iid = self.table.identify_row(event.y)
        if not selected_iid: return
        
        self.table.selection_set(selected_iid)
        
        try:
            download_id = int(self.table.item(selected_iid, "values")[0])
        except (IndexError, ValueError):
            logging.error("ContextMenu: Could not get valid download ID from selected row.")
            return
            
        item = self.downloads.get(download_id)
        if not item:
            logging.warning(f"ContextMenu: Download item with ID {download_id} not found.")
            return

        menu = tk.Menu(self, tearoff=0)
        
        if item.status == "Downloading":
            menu.add_command(label="⏸️ Pause", command=lambda id=download_id: self.pause_specific_download(id))
        elif item.status in ["Paused", "Pending", "Stopped", "Error", "Cancelled"] or "Error" in item.status:
            menu.add_command(label="▶️ Start/Resume", command=lambda id=download_id: self.start_or_resume_download(id))
        
        if item.status in ["Downloading", "Paused", "Connecting...", "Resuming...", "Retrying..."]:
            menu.add_command(label="⏹️ Stop", command=lambda id=download_id: self.stop_specific_download(id))

        if item.status in ["Completed", "Error"] or "Error" in item.status:
            menu.add_command(label="♻️ Redownload", command=lambda id=download_id: self.redownload_item(id))

        if menu.index(tk.END) is not None:
            menu.add_separator()

        menu.add_command(label="📄 Show Info", command=lambda id=download_id: self.show_file_info_dialog(id))
        menu.add_command(label="📂 Open Containing Folder", command=lambda id=download_id: self.open_containing_folder(id))
        menu.add_command(label="🔗 Copy URL", command=lambda id=download_id: self.copy_url_to_clipboard(id))
        menu.add_separator()
        menu.add_command(label="❌ Delete Entry (Keep File)", command=lambda id=download_id: self.delete_specific_download(id, delete_file=False))
        menu.add_command(label="❌ Delete Entry AND File(s)", command=lambda id=download_id: self.delete_specific_download(id, delete_file=True))
        
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def open_containing_folder(self, download_id: int):
        item = self.downloads.get(download_id)
        if item and item.save_path:
            folder_path = os.path.dirname(item.save_path)
            if os.path.isdir(folder_path):
                try:
                    if sys.platform == "win32":
                        os.startfile(folder_path)
                    elif sys.platform == "darwin":
                        subprocess.Popen(["open", folder_path])
                    else:
                        subprocess.Popen(["xdg-open", folder_path])
                    self.status_bar_label.config(text=f"Opened folder: {folder_path}")
                except Exception as e:
                    logging.error(f"Error opening folder '{folder_path}': {e}", exc_info=True)
                    messagebox.showerror("Error", f"Could not open folder: {e}", parent=self)
            else:
                messagebox.showwarning("Folder Not Found", f"The folder '{folder_path}' does not exist.", parent=self)
        else:
            messagebox.showinfo("Info", "Save path is not available for this item.", parent=self)

    def copy_url_to_clipboard(self, download_id: int):
        item = self.downloads.get(download_id)
        if item:
            try:
                pyperclip.copy(item.url)
                self.status_bar_label.config(text=f"URL copied for '{os.path.basename(item.save_path)}'")
                messagebox.showinfo("URL Copied", "The download URL has been copied to the clipboard.", parent=self)
            except pyperclip.PyperclipException as e:
                logging.error(f"Error copying URL to clipboard: {e}", exc_info=True)
                messagebox.showerror("Clipboard Error", f"Could not copy URL to clipboard: {e}", parent=self)
        else:
            messagebox.showinfo("Info", "Download item not found.", parent=self)

    def _on_table_double_click_show_info(self, event=None):
        selected_iids = self.table.selection()
        if not selected_iids:
            self.status_bar_label.config(text="No item selected to show info.")
            return
        try:
            download_id = int(self.table.item(selected_iids[0], "values")[0])
            self.show_file_info_dialog(download_id)
        except (IndexError, ValueError):
            logging.error("ShowInfo (double-click): Could not get a valid ID from the selected table row.")
            self.status_bar_label.config(text="Error: Invalid item selected.")

    def show_file_info_dialog(self, download_id: int):
        if download_id in self.active_file_info_dialogs and self.active_file_info_dialogs[download_id].winfo_exists():
            self.active_file_info_dialogs[download_id].lift()
            self.active_file_info_dialogs[download_id].focus_set()
            return

        item_to_show = self.downloads.get(download_id)
        if item_to_show:
            dialog = FileInfoDialog(self, download_id, item_to_show)
            self.active_file_info_dialogs[download_id] = dialog
        else:
            logging.warning(f"ShowFileInfo: Download ID {download_id} not found in active downloads.")
            self.status_bar_label.config(text=f"Error: Download item with ID {download_id} no longer exists.")

    def on_file_info_dialog_close(self, download_id: int):
        if download_id in self.active_file_info_dialogs:
            del self.active_file_info_dialogs[download_id]
            logging.debug(f"File info dialog for ID {download_id} closed and removed from tracking.")

    def _process_asyncio_tasks(self):
        if not self.winfo_exists() or self.loop.is_closed():
            return
        try:
            self.loop.stop()
            self.loop.run_forever()
        except RuntimeError as e:
            if "cannot schedule new futures after shutdown" not in str(e).lower() and \
               "event loop is closed" not in str(e).lower():
                logging.debug(f"Asyncio task processing error: {e}")
        except KeyboardInterrupt:
            self._on_closing_attempt()
        
        if self.winfo_exists() and not self.loop.is_closed():
            self.after(50, self._process_asyncio_tasks)

    def _check_clipboard_for_urls(self):
        if not self.winfo_exists(): return
        try:
            current_clipboard = pyperclip.paste()
        except pyperclip.PyperclipException as e:
            logging.warning(f"Could not access clipboard: {e}. Clipboard monitoring might be affected.")
            if self.winfo_exists(): self.after(5000, self._check_clipboard_for_urls)
            return

        if current_clipboard != self.last_clipboard_content:
            self.last_clipboard_content = current_clipboard
            if validators.url(current_clipboard) and \
               not any(d.url == current_clipboard for d in self.downloads.values()):
                if messagebox.askyesno("URL Detected in Clipboard",
                                       f"The following URL was detected in your clipboard:\n\n{current_clipboard}\n\n"
                                       "Would you like to add it as a new download?", parent=self):
                    self.show_add_download_dialog(current_clipboard)
        
        if self.winfo_exists():
            self.after(1000, self._check_clipboard_for_urls)

    def show_add_download_dialog(self, initial_url: str = ""):
        if not self.downloader or not self.downloader.aria2c_path:
            messagebox.showerror("Downloader Not Ready",
                                 "The aria2c downloader component is not available. Cannot add downloads.",
                                 parent=self)
            return

        dialog = AddDownloadDialog(self, initial_url=initial_url)
        if dialog.result:
            new_url, save_path = dialog.result
            if any(item.url == new_url and item.save_path == save_path for item in self.downloads.values()):
                messagebox.showinfo("Duplicate Download",
                                    "A download with the same URL and save path already exists.", parent=self)
                return
            
            self.add_new_download_item(new_url, save_path, start_immediately=True)
            self.status_bar_label.config(text=f"Added new download: {os.path.basename(save_path)}")

    def add_new_download_item(self, url: str, save_path: str, start_immediately: bool = True):
        new_item = DownloadItem(url, save_path)
        download_id = self.db.add_download(new_item)

        if download_id is None:
            logging.error(f"Failed to add download to database: {url} -> {save_path}")
            messagebox.showerror("Database Error", "Could not add the download to the database. It might be a duplicate.", parent=self)
            return

        self.downloads[download_id] = new_item
        logging.info(f"Added new download ID {download_id}: {url} to {save_path}")
        self._add_or_update_item_in_table(download_id, new_item)

        if start_immediately:
            self.start_or_resume_download(download_id)
        else:
            new_item.status = "Pending"
            self.schedule_update_download_progress(download_id, new_item.progress, new_item.speed, new_item.eta, new_item.status, new_item.file_size)
        self._update_action_buttons_state()

    def start_or_resume_download(self, download_id: int):
        item = self.downloads.get(download_id)
        if not item:
            logging.warning(f"Start/Resume: Download ID {download_id} not found.")
            return
        
        if not self.downloader or not self.downloader.aria2c_path:
            messagebox.showerror("Downloader Not Ready",
                                 "The aria2c downloader component is not available. Cannot start download.",
                                 parent=self)
            item.status = "Error: Misconfig"
            self.schedule_update_download_progress(download_id, item.progress, 0,0,item.status, item.file_size)
            return

        if item.task and not item.task.done():
            if item.is_paused:
                item.is_paused = False
                logging.info(f"Resuming download ID {download_id}.")
                self.schedule_update_download_progress(download_id, item.progress, item.speed, item.eta, "Resuming...", item.file_size)
            else:
                logging.info(f"Download ID {download_id} is already running or connecting.")
            self._update_action_buttons_state()
            return

        item.is_paused = False
        item.is_stopped = False
        item.status = "Connecting..."
        self.schedule_update_download_progress(download_id, item.progress, item.speed, item.eta, item.status, item.file_size)
        
        if item.task and not item.task.done():
            item.task.cancel()

        item.task = self.loop.create_task(self.downloader.download(download_id, item))
        logging.debug(f"Scheduled asyncio download task for ID {download_id}.")
        self.status_bar_label.config(text=f"Starting download: {os.path.basename(item.save_path)}")
        self._update_action_buttons_state()

    def pause_specific_download(self, download_id: int):
        item = self.downloads.get(download_id)
        if item and item.status == "Downloading" and not item.is_paused:
            item.is_paused = True
            logging.info(f"Signaled PAUSE for download ID {download_id}.")
            self.status_bar_label.config(text=f"Pausing download: {os.path.basename(item.save_path)}")
        self._update_action_buttons_state()

    def stop_specific_download(self, download_id: int):
        item = self.downloads.get(download_id)
        if item and item.status not in ["Completed", "Stopped", "Cancelled"]:
            item.is_stopped = True
            item.is_paused = False

            logging.info(f"Signaled STOP for download ID {download_id}.")
            self.status_bar_label.config(text=f"Stopping download: {os.path.basename(item.save_path)}")

            if item.task and not item.task.done():
                item.task.cancel()
            else:
                item.status = "Stopped"
                self.db.update_download(download_id, item.progress, item.status, item.downloaded, item.is_paused, item.file_size)
                self._add_or_update_item_in_table(download_id, item)
        
        self._update_action_buttons_state()

    def _toggle_selected_download(self):
        selected_iids = self.table.selection()
        if not selected_iids:
            self.status_bar_label.config(text="No download selected to toggle.")
            return
        try:
            download_id = int(self.table.item(selected_iids[0], "values")[0])
        except (IndexError, ValueError): return

        item = self.downloads.get(download_id)
        if not item: return

        startable_statuses = ["Pending", "Stopped", "Error", "Completed", "Paused", "Cancelled"]
        startable_statuses.extend([s for s in item.status if "Error" in s or "RC" in s])

        if item.status == "Downloading":
            self.pause_specific_download(download_id)
        elif item.status in startable_statuses or "Error" in item.status:
            if item.status == "Completed":
                self.redownload_item(download_id)
            else:
                self.start_or_resume_download(download_id)
        self._update_action_buttons_state()

    def _stop_selected_download_action(self):
        selected_iids = self.table.selection()
        if not selected_iids:
            self.status_bar_label.config(text="No download selected to stop.")
            return
        try:
            download_id = int(self.table.item(selected_iids[0], "values")[0])
            self.stop_specific_download(download_id)
        except (IndexError, ValueError): return

    def delete_specific_download(self, download_id: int, delete_file: bool):
        item_to_delete = self.downloads.get(download_id)
        if not item_to_delete:
            logging.warning(f"Attempted to delete non-existent download ID {download_id}.")
            return

        if item_to_delete.status in ["Downloading", "Paused", "Connecting...", "Resuming...", "Retrying..."]:
            item_to_delete.is_stopped = True
            item_to_delete.is_paused = False
            if item_to_delete.task and not item_to_delete.task.done():
                item_to_delete.task.cancel()

        if download_id in self.active_file_info_dialogs:
            dialog = self.active_file_info_dialogs.pop(download_id)
            if dialog and dialog.winfo_exists():
                dialog.destroy()

        display_name = os.path.basename(item_to_delete.save_path) or item_to_delete.url

        if delete_file:
            paths_to_delete = [item_to_delete.save_path]
            control_file = item_to_delete.save_path + ".aria2"
            if os.path.exists(control_file):
                paths_to_delete.append(control_file)
            
            deleted_files_count = 0
            for path_to_remove in paths_to_delete:
                if os.path.exists(path_to_remove):
                    try:
                        os.remove(path_to_remove)
                        logging.info(f"Deleted file: {path_to_remove}")
                        deleted_files_count +=1
                    except OSError as e:
                        logging.error(f"Error deleting file '{path_to_remove}': {e}", exc_info=True)
                        messagebox.showerror("Delete Error", f"Could not delete file: {path_to_remove}\n{e}", parent=self)
            if deleted_files_count > 0:
                self.status_bar_label.config(text=f"Deleted entry and file(s) for: {display_name}")
            else:
                self.status_bar_label.config(text=f"Removed entry for: {display_name} (file not found or not deleted).")

        else:
            self.status_bar_label.config(text=f"Removed entry for: {display_name}")

        if download_id in self.downloads:
            del self.downloads[download_id]
        self.db.delete_download(download_id)
        logging.debug(f"Deleted download ID {download_id} from records.")
        
        self._remove_item_from_table(download_id)
        self._update_action_buttons_state()

    def _delete_selected_download_with_prompt(self):
        selected_iids = self.table.selection()
        if not selected_iids:
            self.status_bar_label.config(text="No download selected to delete.")
            return
        try:
            download_id = int(self.table.item(selected_iids[0], "values")[0])
        except (IndexError, ValueError): return

        item = self.downloads.get(download_id)
        if not item: return
        
        display_name = os.path.basename(item.save_path) or item.url
        delete_associated_file = False

        if os.path.exists(item.save_path) and not item.save_path.endswith(".aria2"):
            confirm_choice = messagebox.askyesnocancel(
                "Confirm Delete",
                f"Delete entry for '{display_name}'?",
                detail="Choose an option:\n"
                       " • Yes: Remove entry, KEEP downloaded file(s).\n"
                       " • No:  Remove entry AND DELETE downloaded file(s).\n"
                       " • Cancel: Do nothing.",
                icon=messagebox.WARNING,
                parent=self
            )
            if confirm_choice is None:
                return
            delete_associated_file = (confirm_choice == False)
        else:
            if not messagebox.askyesno(
                "Confirm Delete Entry",
                f"Delete entry for '{display_name}'?\n\n(File not found or is an auxiliary file. Only the entry will be removed).",
                parent=self):
                return

        self.delete_specific_download(download_id, delete_file=delete_associated_file)

    def redownload_item(self, download_id: int):
        old_item = self.downloads.get(download_id)
        if not old_item:
            logging.warning(f"Redownload: Item ID {download_id} not found.")
            return
        
        url_to_redownload = old_item.url
        path_to_redownload = old_item.save_path
        
        self.delete_specific_download(download_id, delete_file=False)
        
        logging.info(f"Initiating redownload for URL: {url_to_redownload} to path: {path_to_redownload}")
        self.add_new_download_item(url_to_redownload, path_to_redownload, start_immediately=True)
        self.status_bar_label.config(text=f"Re-downloading: {os.path.basename(path_to_redownload)}")

    def _on_table_selection_change(self, event=None):
        self._update_action_buttons_state()

    def _update_action_buttons_state(self):
        selected_iids = self.table.selection()
        if not selected_iids:
            self.toggle_button.config(text="▶️ Start", state="disabled")
            self.stop_button.config(state="disabled")
            return

        try:
            download_id = int(self.table.item(selected_iids[0], "values")[0])
            item = self.downloads.get(download_id)
        except (IndexError, ValueError, TypeError):
            item = None

        if item:
            self.toggle_button.config(state="normal")
            self.stop_button.config(state="normal" if item.status in ["Downloading", "Connecting...", "Resuming...", "Retrying..."] else "disabled")

            if item.status == "Downloading":
                self.toggle_button.config(text="⏸️ Pause")
            elif item.status == "Paused":
                self.toggle_button.config(text="▶️ Resume")
            elif item.status == "Completed":
                self.toggle_button.config(text="♻️ Redownload")
            elif item.status in ["Pending", "Stopped", "Error", "Cancelled"] or "Error" in item.status or "RC" in item.status:
                self.toggle_button.config(text="▶️ Start")
            elif item.status in ["Connecting...", "Resuming...", "Retrying...", "Stopping...", "Pausing..."]:
                self.toggle_button.config(text="⏳ Busy...", state="disabled")
            else:
                self.toggle_button.config(text="▶️ Start", state="disabled")
                self.stop_button.config(state="disabled")
        else:
            self.toggle_button.config(text="▶️ Start", state="disabled")
            self.stop_button.config(state="disabled")

    def schedule_update_download_progress(self, download_id: int, progress: float, speed: float, eta: int, status: str, file_size: Optional[int]):
        if self.winfo_exists():
            self.after(0, self._perform_safe_gui_update, download_id, progress, speed, eta, status, file_size)

    def _perform_safe_gui_update(self, download_id: int, progress: float, speed: float, eta: int, status: str, file_size: Optional[int]):
        if download_id not in self.downloads:
            return

        item = self.downloads[download_id]
        
        item.progress = max(0.0, min(100.0, progress))
        item.speed = speed
        item.eta = eta
        
        critical_statuses = ["Completed", "Error", "Stopped", "Paused", "Cancelled",
                             "Connecting...", "Resuming...", "Retrying...", "Stopping...", "Pausing..."]
        if item.status != status or status in critical_statuses or "Error" in status:
            item.status = status

        if file_size is not None and file_size > 0:
            if item.file_size <= 0 or item.file_size != file_size:
                item.file_size = file_size
        
        if item.file_size > 0:
            item.downloaded = int((item.progress / 100.0) * item.file_size)

        self.db.update_download(download_id, item.progress, item.status, item.downloaded, item.is_paused, item.file_size)
        
        self._add_or_update_item_in_table(download_id, item)
        
        selected_iids = self.table.selection()
        if selected_iids and int(self.table.item(selected_iids[0], "values")[0]) == download_id:
            self._update_action_buttons_state()
            if "Completed" in status:
                self.status_bar_label.config(text=f"Completed: {os.path.basename(item.save_path)}")
            elif "Error" in status:
                self.status_bar_label.config(text=f"Error with: {os.path.basename(item.save_path)}. Check logs.")
            elif status == "Downloading":
                self.status_bar_label.config(text=f"Downloading: {os.path.basename(item.save_path)} at {item.speed:.2f} Mbps")

    def show_error(self, message: str):
        if self.winfo_exists():
            messagebox.showerror("Download Process Error", message, parent=self)
        logging.error(f"Error shown to user: {message}")
        self.status_bar_label.config(text="An error occurred. Please check logs.")
        self._update_action_buttons_state()

    def load_downloads_from_db(self):
        self.downloads.clear()
        for iid in self.table.get_children():
            self.table.delete(iid)

        db_items = self.db.get_downloads()
        if not db_items:
            logging.info("No previous downloads found in the database.")
            return

        for db_row in db_items:
            dl_id, url, save_path, file_size, status, progress_val, downloaded_val, is_paused_int = db_row
            
            item = DownloadItem(url, save_path, file_size if file_size else 0)
            item.status = status if status else "Unknown"
            item.progress = progress_val if progress_val else 0.0
            item.downloaded = downloaded_val if downloaded_val else 0
            item.is_paused = bool(is_paused_int)

            active_on_close_statuses = ["Downloading", "Resuming...", "Connecting...", "Retrying...", "Pausing...", "Stopping..."]
            if item.status in active_on_close_statuses:
                item.status = "Paused"
                item.is_paused = True
                self.db.update_download(dl_id, item.progress, item.status, item.downloaded, item.is_paused, item.file_size)

            elif item.status == "Pending Start":
                item.status = "Pending"
            
            self.downloads[dl_id] = item
            self._add_or_update_item_in_table(dl_id, item)
        
        logging.info(f"Loaded {len(self.downloads)} download items from the database.")
        self._update_action_buttons_state()

    def _add_or_update_item_in_table(self, download_id: int, item: DownloadItem):
        filename = os.path.basename(item.save_path) or "N/A"
        size_mb_str = f"{item.file_size / (1024*1024):.2f}" if item.file_size and item.file_size > 0 else "0.00"
        eta_str = f"{int(item.eta)}" if item.eta >= 0 else "---"
        
        progress_display = f"{item.progress:.1f}%"
        if item.status == "Downloading":
            bar_len = 10
            filled_len = int(bar_len * item.progress / 100)
            bar = '█' * filled_len + '-' * (bar_len - filled_len)
            progress_display = f"{bar} {item.progress:.1f}%"

        values = (
            str(download_id),
            filename,
            size_mb_str,
            progress_display,
            f"{item.speed:.2f}",
            eta_str,
            item.status,
            item.save_path
        )
        iid_str = str(download_id)

        if self.table.exists(iid_str):
            self.table.item(iid_str, values=values)
        else:
            self.table.insert("", tk.END, iid=iid_str, values=values)

    def _remove_item_from_table(self, download_id: int):
        iid_str = str(download_id)
        if self.table.exists(iid_str):
            self.table.delete(iid_str)

    def _update_ui_periodically(self):
        if not self.winfo_exists(): return
        
        self.after(1000, self._update_ui_periodically)

    async def _cleanup_async_tasks_on_close(self):
        logging.info("Initiating cleanup of active asyncio tasks before closing...")
        tasks_to_await = []
        
        for item_id, item in list(self.downloads.items()):
            if item.task and not item.task.done():
                logging.info(f"Cleanup: Signaling stop and cancelling task for download ID {item_id}.")
                item.is_stopped = True
                item.task.cancel()
                tasks_to_await.append(item.task)
            
            active_statuses = ["Downloading", "Connecting...", "Resuming...", "Retrying...", "Pausing...", "Stopping..."]
            if item.status in active_statuses:
                item.status = "Paused"
                item.is_paused = True
                self.db.update_download(item_id, item.progress, item.status, item.downloaded, item.is_paused, item.file_size)
                logging.info(f"Cleanup: Marked download ID {item_id} as 'Paused' in database.")
        
        if tasks_to_await:
            logging.info(f"Cleanup: Waiting for {len(tasks_to_await)} download tasks to complete cancellation...")
            await asyncio.gather(*tasks_to_await, return_exceptions=True)
        
        logging.info("Cleanup: Async tasks processing finished.")

    def _on_closing_attempt(self):
        logging.info("Application closing process initiated by user (WM_DELETE_WINDOW).")
        if messagebox.askokcancel("Quit AsyncDADAloader", "Are you sure you want to exit AsyncDADAloader?", parent=self):
            self.status_bar_label.config(text="Exiting... Please wait for cleanup...")
            self.update_idletasks()

            if self.loop.is_running():
                cleanup_future = asyncio.run_coroutine_threadsafe(self._cleanup_async_tasks_on_close(), self.loop)
                try:
                    cleanup_future.result(timeout=10.0)
                    logging.info("Async cleanup tasks completed within timeout.")
                except TimeoutError:
                    logging.warning("Async cleanup tasks timed out. Some tasks might not have finished cleanly.")
                except Exception as e:
                    logging.error(f"Exception during async cleanup: {e}", exc_info=True)
                finally:
                    pass
            else:
                try:
                    if not self.loop.is_closed():
                        self.loop.run_until_complete(self._cleanup_async_tasks_on_close())
                        logging.info("Async cleanup tasks completed (loop was not running).")
                except RuntimeError as e:
                    logging.warning(f"Could not run cleanup (loop not running or already closed): {e}")
            
            if self.db:
                self.db.close()
            
            self.after(200, self._perform_actual_destroy)
        else:
            logging.info("User cancelled application quit.")

    def _perform_actual_destroy(self):
        logging.info("Performing actual destruction of the Tkinter window.")
        self.destroy()

        if self.loop and not self.loop.is_closed():
            logging.info("Finalizing asyncio event loop closure.")
            try:
                if self.loop.is_running():
                    self.loop.call_soon_threadsafe(self.loop.stop)
                
                pending_tasks = asyncio.all_tasks(loop=self.loop)
                if pending_tasks:
                    logging.info(f"Cancelling {len(pending_tasks)} remaining tasks in the loop.")
                    for task in pending_tasks:
                        task.cancel()
                    self.loop.run_until_complete(asyncio.gather(*pending_tasks, return_exceptions=True))

                self.loop.run_until_complete(self.loop.shutdown_asyncgens())
                self.loop.close()
                logging.info("Asyncio event loop closed successfully.")
            except RuntimeError as e:
                logging.warning(f"Error during final asyncio loop closure: {e}", exc_info=True)
        
        logging.info(f"{APP_NAME} window destroyed and application exiting.")

def main():
    if sys.platform == "win32" and sys.version_info >= (3, 8):
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    try:
        evt_loop = asyncio.get_event_loop()
    except RuntimeError:
        evt_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(evt_loop)

    root_window = None
    try:
        root_window = AsyncDADAloaderWindow(evt_loop)
        
        if hasattr(root_window, '_is_destroying_startup') and root_window._is_destroying_startup:
            logging.info("Main: Startup error detected. Window is being destroyed. Mainloop will not start.")
            if not (srgb_to_xyz_d65ys.platform == "win32" and sys.version_info >= (3,8)):
                pass

        else:
            logging.info("Main: Starting Tkinter mainloop.")
            root_window.mainloop()

    except SystemExit as se:
        logging.info(f"Main: Application exited via SystemExit (code: {se.code}).")
    except tk.TclError as e:
        if "application has been destroyed" in str(e).lower():
            logging.info(f"Main: Tkinter TclError (application destroyed), likely normal exit: {e}")
        else:
            logging.critical(f"Main: Unhandled Tkinter TclError: {e}", exc_info=True)
            try: messagebox.showerror("Fatal Tkinter Error", f"A critical Tkinter error occurred: {e}\nThe application will close. Check logs: {LOG_FILE_PATH}")
            except: pass
    except Exception as e:
        logging.critical(f"Main: Unhandled top-level exception: {e}", exc_info=True)
        if root_window and root_window.winfo_exists():
            messagebox.showerror("Fatal Error", f"A critical error occurred: {e}\nThe application will close. Please check the logs at: {LOG_FILE_PATH}", parent=root_window)
        else:
            print(f"FATAL ERROR: {e}. Check logs at {LOG_FILE_PATH}")
    finally:
        logging.info("Main: Application finally block reached. Ensuring resources are cleaned up.")
        
        if evt_loop and evt_loop.is_running():
            logging.warning("Main finally: Asyncio event loop is still running. Forcing stop.")
            evt_loop.stop()
        
        if evt_loop and not evt_loop.is_closed():
            logging.info("Main finally: Asyncio event loop is not closed. Attempting graceful shutdown.")
            try:
                all_tasks = asyncio.all_tasks(loop=evt_loop)
                if all_tasks:
                    logging.info(f"Main finally: Cancelling {len(all_tasks)} outstanding asyncio tasks.")
                    for task in all_tasks:
                        task.cancel()
                    evt_loop.run_until_complete(asyncio.gather(*all_tasks, return_exceptions=True))

                evt_loop.run_until_complete(evt_loop.shutdown_asyncgens())
            except RuntimeError as e_ru:
                logging.warning(f"Main finally: Runtime error during asyncgen shutdown: {e_ru}")
            except Exception as e_gen:
                logging.error(f"Main finally: Exception during asyncgen shutdown: {e_gen}", exc_info=True)
            finally:
                if not evt_loop.is_closed():
                    evt_loop.close()
                    logging.info("Main finally: Asyncio event loop closed.")
                else:
                    logging.info("Main finally: Asyncio event loop was already closed.")
        else:
            logging.info("Main finally: Asyncio event loop was not active or already closed.")
            
        logging.info(f"--- {APP_NAME} Session Ended ---")

if __name__ == "__main__":
    main()
