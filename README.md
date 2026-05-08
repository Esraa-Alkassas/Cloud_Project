<<<<<<< HEAD
# 🚀 ESP32 WiFi Validation Project: Getting Started

Welcome to the team! This repository is your introduction to ESP32 development using the official ESP-IDF (Espressif IoT Development Framework). 

We use **Docker**, **Ubuntu (WSL)**, and **VS Code Dev Containers** to ensure everyone has the exact same development environment. This means you will never have to worry about missing drivers, wrong compiler versions, or cluttered host machines. 

This guide is broken into two parts: **First-Time Setup** (done once) and **Daily Workflow** (what you do every day). Follow the steps exactly as written.

---

# 🛠️ PART 1: First-Time Setup (Do This Once)

This setup takes about 20-30 minutes. Take your time and pay attention to which terminal you should be using for each step.
* 🪟 **Windows PowerShell:** Run as Administrator.
* 🐧 **Ubuntu Terminal:** The Linux environment running inside Windows.
* 💻 **VS Code Terminal:** The terminal inside your code editor.

### Step 1: Install Core Windows Software
1. **VS Code:** Download and install from [code.visualstudio.com](https://code.visualstudio.com/). Stick to the default options.
2. **Docker Desktop:** Download and install from [docker.com](https://www.docker.com/products/docker-desktop). Ensure "Use WSL 2 instead of Hyper-V" is checked during installation.
3. **USB Passthrough Tool:** Open a 🪟 **Windows PowerShell (Run as Administrator)** and run:
   ```powershell
   winget install --interactive --exact dorssel.usbipd-win
   ```

### Step 2: Install Ubuntu (The Linux Engine)
We need a standard Linux environment to handle our USB drivers properly.
1. In your 🪟 **Administrator PowerShell**, install Ubuntu:
   ```powershell
   wsl --install -d Ubuntu
   ```
2. Restart your computer if Windows prompts you to do so.
3. Open the **Ubuntu** app from your Windows Start Menu. It will take a minute to initialize and will ask you to create a UNIX username and password. Remember this password; you will need it later.

### Step 3: Link Docker to Ubuntu
We must tell Docker to use your new Ubuntu system to build our code.
1. Open the **Docker Desktop** application on Windows.
2. Click the **Gear icon (Settings)** in the top right.
3. Go to **Resources** -> **WSL Integration** on the left menu.
4. Turn the toggle switch for **Ubuntu** to **ON**.
5. Click **Apply & restart** in the bottom right. Keep Docker running in the background.

### Step 4: Generate an SSH Key & Get the Code
You must download the code *inside* Ubuntu, not on your regular Windows C: drive. This makes compiling significantly faster.

1. Open your 🐧 **Ubuntu Terminal** and generate a security key:
   ```bash
   ssh-keygen -t rsa -b 4096 -C "your_email@work.com"
   ```
   *(Press `Enter` to accept all defaults. Do not type a password).*
2. Display your new key:
   ```bash
   cat ~/.ssh/id_rsa.pub
   ```
3. Copy the output text (starts with `ssh-rsa`). Go to **GitHub -> Settings -> SSH and GPG keys -> New SSH key**, paste it in, and save.
4. Create a workspace folder and download the code:
   ```bash
   mkdir ~/projects
   cd ~/projects
   git clone git@github.com:Esraa-Alkassas/Cloud_Project.git
   ```

### Step 5: Launch VS Code & Build the Environment
1. Still inside your 🐧 **Ubuntu Terminal**, navigate to the project and open VS Code:
   ```bash
   cd ~/projects/Cloud_Project
   code .
   ```
2. VS Code will open. A prompt will appear in the bottom right: *"Folder contains a Dev Container configuration file"*. Click **Reopen in Container**.
   *(If you miss it, press `F1`, type `Dev Containers: Reopen in Container`, and hit Enter).*
3. Go grab a coffee. ☕ VS Code is downloading the ESP-IDF tools. This takes 30 minutes the first time (based on internet speed).

### Step 6: Configure WiFi Credentials
Once the container finishes building, open a terminal in VS Code (`Terminal` -> `New Terminal`). You are now inside the 💻 **VS Code DevContainer**.

1. Open the configuration menu:
   ```bash
   idf.py menuconfig
   ```
2. Use your arrow keys to navigate to **WiFi Configuration** and press `Enter`.
3. Select **WiFi SSID**, press `Enter`, type your 2.4 GHz network name, and press `Enter`.
4. Select **WiFi Password**, press `Enter`, type your password, and press `Enter`.
5. Press `S` to Save, `Enter` to confirm, and `Q` to Quit.

***

# ☀️ PART 2: Daily Developer Workflow

You have survived the setup! From now on, whenever you sit down to work on this project, this is all you have to do.

### Step 1: Connect the ESP32
1. Plug your ESP32 board into your computer via USB.
2. Open a 🪟 **Windows PowerShell (Administrator)** and find your board's ID:
   ```powershell
   usbipd list
   ```
3. Look for your device (usually called "CP2102", "CH340", or "Silicon Labs") and note its `BUSID` (e.g., `1-5`).
4. Attach it to Ubuntu:
   ```powershell
   usbipd attach --wsl Ubuntu --busid <YOUR_BUSID>
   ```

### Step 2: Open the Project & Code
1. Open your 🐧 **Ubuntu Terminal** and launch the project:
   ```bash
   cd ~/projects/Cloud_Project
   code .
   ```
   *(VS Code will remember it is a DevContainer and boot up your ESP-IDF tools automatically).*

### Step 3: Build, Flash, and Monitor
1. Open a new terminal inside VS Code (💻 **VS Code DevContainer**).
2. Compile your code:
   ```bash
   idf.py build
   ```
3. Flash the code to the ESP32 and open the serial monitor to see the output:
   ```bash
   idf.py flash monitor
   ```

**What to expect:**
In the monitor, you will see the board boot up. Look for these lines:
```text
I (850) wifi_init: initializing...
I (1500) wifi: connected with myssid, channel 6
I (2500) example_wifi: Successfully connected to access point!
```
If you see the successful connection message, your environment is perfect and you are ready to write code.

*(To exit the serial monitor at any time, press `Ctrl` + `]`)*
=======
# Cloud_Project
This is the repo for the cloud research project.
# Update Test
<<<<<<< HEAD
>>>>>>> 06bbfd1 (Test Lambda validation)
=======
>>>>>>> 8c92b6fe358d5b82689eaac3a1cfea989faeea3a
