# VERONICA + ROS2 Demo

Runtime containment for ROS2 nodes using veronica-core.

## What This Demo Shows

A TurtleBot3 drives autonomously in Gazebo. When the LiDAR sensor is corrupted:

1. **CircuitBreaker** detects repeated faults and opens
2. **OperatingMode** degrades: FULL_AUTO -> SLOW -> HALT
3. Robot slows down and eventually stops
4. When the sensor recovers, the circuit closes and the robot resumes

## Prerequisites

```bash
# Ubuntu 24.04 + ROS2 Jazzy
sudo apt install ros-jazzy-desktop ros-jazzy-turtlebot3-gazebo ros-jazzy-turtlebot3-navigation2
pip install veronica-core
```

## Run

```bash
# Terminal 1: Gazebo simulation
export TURTLEBOT3_MODEL=burger
ros2 launch turtlebot3_gazebo turtlebot3_world.launch.py

# Terminal 2: VERONICA safety node
source /opt/ros/jazzy/setup.bash
python3 turtlebot_safety_demo.py

# Terminal 3: Inject fault (trigger degradation)
ros2 topic pub /scan_fault std_msgs/Bool "data: true" --once

# Terminal 4: Clear fault (trigger recovery)
ros2 topic pub /scan_fault std_msgs/Bool "data: false" --once
```

## Expected Output

```
[VERONICA] Safety demo node started.
[VERONICA] Fault suppressed: SensorFault('LiDAR corruption: 100% NaN readings')
[VERONICA] Fault suppressed: SensorFault(...)
[VERONICA] === MODE CHANGE: FULL_AUTO -> HALT ===
[VERONICA] Mode transition: FULL_AUTO -> HALT (speed_scale=0.0)
...
[VERONICA] Fault injection CLEARED
[VERONICA] === MODE CHANGE: HALT -> SLOW ===
[VERONICA] Mode transition: HALT -> SLOW (speed_scale=0.15)
[VERONICA] === MODE CHANGE: SLOW -> FULL_AUTO ===
[VERONICA] Mode transition: SLOW -> FULL_AUTO (speed_scale=1.0)
```
