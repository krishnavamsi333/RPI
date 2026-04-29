#!/bin/bash

set -e

LOG_FILE="setup_log.txt"
exec > >(tee -i $LOG_FILE) 2>&1

echo "Updating system packages..."
sudo apt update && sudo apt upgrade -y

echo "Installing base development tools..."
sudo apt install -y \
  build-essential cmake git curl wget unzip pkg-config \
  htop neovim net-tools tmux screen \
  openssh-server avahi-daemon

echo "Enabling SSH service..."
sudo systemctl enable ssh
sudo systemctl start ssh

echo "Installing Python environment..."
sudo apt install -y python3 python3-pip python3-venv
python3 -m pip install --upgrade pip

echo "Installing Python libraries..."
python3 -m pip install \
  numpy scipy matplotlib opencv-python pyserial

echo "Setting up ROS 2 repository..."
sudo apt install -y software-properties-common
sudo add-apt-repository universe -y

sudo apt update
sudo apt install -y curl gnupg lsb-release

sudo mkdir -p /etc/apt/keyrings
curl -sSL https://raw.githubusercontent.com/ros/rosdistro/master/ros.key \
  | sudo gpg --dearmor -o /etc/apt/keyrings/ros-archive-keyring.gpg

echo "deb [signed-by=/etc/apt/keyrings/ros-archive-keyring.gpg] http://packages.ros.org/ros2/ubuntu \
$(. /etc/os-release && echo $UBUNTU_CODENAME) main" \
| sudo tee /etc/apt/sources.list.d/ros2.list > /dev/null

sudo apt update

echo "Installing ROS 2 Humble..."
sudo apt install -y ros-humble-desktop

echo "Configuring ROS environment..."
if ! grep -q "source /opt/ros/humble/setup.bash" ~/.bashrc; then
  echo "source /opt/ros/humble/setup.bash" >> ~/.bashrc
fi
source /opt/ros/humble/setup.bash

echo "Installing ROS development tools..."
sudo apt install -y \
  python3-colcon-common-extensions \
  python3-rosdep \
  python3-rosinstall \
  python3-vcstool

echo "Initializing rosdep..."
sudo rosdep init 2>/dev/null || true
rosdep update

echo "Installing hardware interface tools..."
sudo apt install -y \
  python3-gpiozero \
  python3-rpi.gpio \
  i2c-tools

echo "Installing OpenCV and camera tools..."
sudo apt install -y \
  libopencv-dev \
  python3-opencv \
  libcamera-apps

echo "Installing Gazebo integration..."
sudo apt install -y ros-humble-gazebo-ros-pkgs

echo "Configuring basic security..."
sudo apt install -y ufw fail2ban
sudo ufw --force enable

echo "Creating ROS 2 workspace..."
mkdir -p ~/ros2_ws/src
cd ~/ros2_ws
colcon build

echo "Setup completed successfully."
echo "Run: source ~/.bashrc"
echo "Then: cd ~/ros2_ws && source install/setup.bash"
