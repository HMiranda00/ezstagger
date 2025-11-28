# EZ Stagger Offset

A Blender extension for quickly applying staggered frame offsets to selected keyframes in the Dope Sheet / Action Editor.

## Features

- **Alt+Drag** to apply staggered offsets to selected keyframes by channel
- Visual feedback showing the current stagger offset and number of groups
- Auto-grouping: per F-Curve or per Object/Bone based on selection
- Multiple ordering modes: Outliner-like or by earliest keyframe time

## Requirements

- Blender 5.0 or later

## Installation

### Method 1: Install from ZIP (Recommended)

1. Download the latest `ezstagger.zip` from the releases
2. In Blender, go to **Edit > Preferences > Get Extensions**
3. Click the dropdown arrow and select **Install from Disk...**
4. Select the downloaded `ezstagger.zip` file
5. Enable the extension if not already enabled

### Method 2: Manual Installation

1. Download or clone this repository
2. Copy the `ezstagger` folder to your Blender extensions directory:
   - Windows: `%APPDATA%\Blender Foundation\Blender\5.0\extensions\user_default\`
   - macOS: `~/Library/Application Support/Blender/5.0/extensions/user_default/`
   - Linux: `~/.config/blender/5.0/extensions/user_default/`
3. Restart Blender and enable the extension in Preferences

## Usage

1. Open the **Dope Sheet** or **Action Editor**
2. Select keyframes you want to stagger
3. Hold **Alt** and **drag left/right** with the mouse
4. The keyframes will be offset in a stair-step pattern by channel

### Modifiers

- **Shift**: Invert grouping mode (per-FCurve vs per-Owner/Bone)
- **Ctrl**: Use time-based ordering instead of Outliner order

### Preferences

Access preferences via **Edit > Preferences > Add-ons > EZ Stagger Offset**:

- **Default Order**: Choose between Outliner-like ordering or time-based ordering
- **Auto Grouping**: Automatically detect grouping level based on channel header selection

## License

GPL-3.0-or-later

