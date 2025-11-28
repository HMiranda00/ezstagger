"""
EZStagger - Development Installation Script

This script automates the installation of the EZStagger extension for development:
1. Closes Blender if running
2. Removes old installation
3. Copies the extension files to Blender's extensions directory
4. Reopens Blender

Usage: python dev/install_addon.py
"""

import os
import shutil
import subprocess
import time


# --- CONFIGURATION ---
EXTENSION_ID = "ezstagger"
BLENDER_VERSION = "5.0"

# Blender 5.0 uses the new extensions system
# Extensions go to: %APPDATA%/Blender Foundation/Blender/5.0/extensions/user_default/
BLENDER_EXTENSIONS_BASE = os.path.join(
    os.environ.get("APPDATA", ""),
    "Blender Foundation",
    "Blender",
    BLENDER_VERSION,
    "extensions",
    "user_default"
)


def get_project_paths():
    """Get the source and destination paths for the extension."""
    # This script is in /dev/, so project root is one level up
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(script_dir, ".."))
    
    # Source: the ezstagger folder containing __init__.py and blender_manifest.toml
    source_path = os.path.join(project_root, EXTENSION_ID)
    
    # Destination: Blender's extensions directory
    destination_path = os.path.join(BLENDER_EXTENSIONS_BASE, EXTENSION_ID)
    
    return source_path, destination_path


def close_blender():
    """Close Blender if it's running."""
    print("--> Checking if Blender is running...")
    os.system("taskkill /F /IM blender.exe >nul 2>&1")
    time.sleep(2)


def remove_old_installation(destination_path):
    """Remove existing installation if present."""
    if os.path.exists(destination_path):
        print(f"--> Removing old installation: {destination_path}")
        try:
            shutil.rmtree(destination_path)
            time.sleep(1)
        except PermissionError:
            print("ERROR: Could not delete folder. Make sure Blender is fully closed.")
            return False
        except Exception as e:
            print(f"ERROR deleting: {e}")
            return False
    else:
        print("--> No previous installation found.")
    return True


def copy_extension(source_path, destination_path):
    """Copy extension files to Blender's extensions directory."""
    print(f"--> Copying from {source_path}...")
    
    if not os.path.exists(source_path):
        print(f"ERROR: Source folder not found: {source_path}")
        print("Make sure the 'ezstagger' folder exists in the project root.")
        return False
    
    # Ensure parent directory exists
    os.makedirs(os.path.dirname(destination_path), exist_ok=True)
    
    try:
        shutil.copytree(
            source_path,
            destination_path,
            ignore=shutil.ignore_patterns(
                '__pycache__',
                '*.pyc',
                '.DS_Store',
            )
        )
        print("--> Copy completed successfully!")
        return True
    except Exception as e:
        print(f"ERROR copying: {e}")
        return False


def open_blender():
    """Open Blender."""
    print("--> Opening Blender...")
    
    try:
        subprocess.Popen(["blender"])
        print("--> Blender started.")
        return True
    except FileNotFoundError:
        print("WARNING: 'blender' command not found in PATH.")
        print("Trying common installation paths...")
        
        possible_paths = [
            rf"C:\Program Files\Blender Foundation\Blender {BLENDER_VERSION}\blender.exe",
            r"C:\Program Files\Blender Foundation\Blender 5.0\blender.exe",
            r"C:\Program Files\Blender Foundation\Blender 4.4\blender.exe",
            r"C:\Program Files\Blender Foundation\Blender 4.3\blender.exe",
        ]
        
        for path in possible_paths:
            if os.path.exists(path):
                subprocess.Popen([path])
                print(f"--> Blender started from: {path}")
                return True
        
        print("ERROR: Could not find Blender executable.")
        print("Please add Blender to your system PATH or configure the path in this script.")
        return False


def main():
    print("=" * 50)
    print(f"  EZStagger Development Installer")
    print(f"  Extension ID: {EXTENSION_ID}")
    print(f"  Blender Version: {BLENDER_VERSION}")
    print("=" * 50)
    print()
    
    source_path, destination_path = get_project_paths()
    
    print(f"Source: {source_path}")
    print(f"Destination: {destination_path}")
    print()
    
    # Step 1: Close Blender
    close_blender()
    
    # Step 2: Remove old installation
    if not remove_old_installation(destination_path):
        return 1
    
    # Step 3: Copy new files
    if not copy_extension(source_path, destination_path):
        return 1
    
    # Step 4: Open Blender
    open_blender()
    
    print()
    print("--> Installation complete!")
    print(f"--> Extension installed to: {destination_path}")
    return 0


if __name__ == "__main__":
    exit(main())
