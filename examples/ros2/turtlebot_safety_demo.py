#!/usr/bin/env python3
"""VERONICA + TurtleBot3 safety demo.

Demonstrates runtime containment for a ROS2 robot:
  - CircuitBreaker detects LiDAR sensor faults (NaN readings)
  - OperatingMode degrades: FULL_AUTO -> SLOW -> HALT
  - On recovery: HALT -> SLOW -> FULL_AUTO

Prerequisites:
  sudo apt install ros-jazzy-turtlebot3-gazebo ros-jazzy-turtlebot3-navigation2
  pip install veronica-core

Run:
  # Terminal 1: Gazebo
  export TURTLEBOT3_MODEL=burger
  ros2 launch turtlebot3_gazebo turtlebot3_world.launch.py

  # Terminal 2: Navigation (optional, for autonomous movement)
  ros2 launch turtlebot3_navigation2 navigation2.launch.py

  # Terminal 3: This script
  python3 turtlebot_safety_demo.py

  # Terminal 4: Inject sensor fault
  ros2 topic pub /scan_fault std_msgs/Bool "data: true" --once
  # Stop fault injection
  ros2 topic pub /scan_fault std_msgs/Bool "data: false" --once
"""

from __future__ import annotations

import math

import rclpy
from geometry_msgs.msg import Twist, TwistStamped
from rclpy.node import Node
from sensor_msgs.msg import LaserScan
from std_msgs.msg import Bool

from veronica_core import CircuitBreaker
from veronica_core.adapters.ros2 import OperatingMode, SafetyMonitor


class SensorFault(Exception):
    """Raised when LiDAR data is corrupted."""


class SafeTurtleBot(Node):
    """TurtleBot3 node with VERONICA runtime containment."""

    # Fraction of NaN readings that triggers a fault
    NAN_THRESHOLD = 0.3
    # Normal cruise speed (m/s)
    CRUISE_SPEED = 0.22
    # Obstacle avoidance: minimum safe distance (m)
    SAFE_DISTANCE = 0.35
    # Turn speed when avoiding obstacles (rad/s)
    TURN_SPEED = 1.0

    def __init__(self) -> None:
        super().__init__("veronica_safety_demo")

        # VERONICA safety primitives
        cb = CircuitBreaker(failure_threshold=5, recovery_timeout=10.0)
        self.safety = SafetyMonitor(
            circuit_breaker=cb,
            logger=self.get_logger(),
            on_mode_change=self._on_mode_change,
        )

        # Subscribers
        self.create_subscription(LaserScan, "/scan", self._on_scan, 10)
        self.create_subscription(Bool, "/scan_fault", self._on_fault_toggle, 10)

        # Publisher for velocity override
        # Gazebo gz-sim bridge expects TwistStamped on /cmd_vel
        self._vel_pub = self.create_publisher(TwistStamped, "/cmd_vel", 10)

        # Fault injection state
        self._fault_active = False

        self.get_logger().info(
            "[VERONICA] Safety demo node started. "
            "Publish to /scan_fault to inject faults."
        )

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def _on_scan(self, msg: LaserScan) -> None:
        """Process each LiDAR scan through the safety monitor."""
        with self.safety.guard(error_type=SensorFault) as mode:
            if mode == OperatingMode.HALT:
                return  # Skip processing entirely when halted

            # Check for injected fault
            if self._fault_active:
                raise SensorFault("Injected sensor fault active")

            # Check for corrupted data
            if self._is_corrupted(msg):
                raise SensorFault(
                    f"LiDAR corruption: "
                    f"{self._nan_fraction(msg):.0%} NaN readings"
                )

            # Apply speed with obstacle avoidance
            self._apply_speed(mode, msg)

    def _on_fault_toggle(self, msg: Bool) -> None:
        """Toggle simulated sensor fault injection."""
        self._fault_active = msg.data
        state = "ACTIVE" if msg.data else "CLEARED"
        self.get_logger().info(f"[VERONICA] Fault injection {state}")

    def _on_mode_change(
        self, old: OperatingMode, new: OperatingMode
    ) -> None:
        """React to operating mode transitions."""
        self.get_logger().warn(
            f"[VERONICA] === MODE CHANGE: {old.name} -> {new.name} ==="
        )
        # Immediately enforce new speed on transition
        self._apply_speed(new)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _is_corrupted(self, msg: LaserScan) -> bool:
        """Detect corrupted LiDAR data (excessive NaN readings)."""
        if not msg.ranges:
            return True
        return self._nan_fraction(msg) > self.NAN_THRESHOLD

    @staticmethod
    def _nan_fraction(msg: LaserScan) -> float:
        """Compute fraction of NaN values in a LaserScan."""
        if not msg.ranges:
            return 1.0
        nan_count = sum(1 for r in msg.ranges if math.isnan(r))
        return nan_count / len(msg.ranges)

    def _min_front_distance(self, scan: LaserScan) -> float:
        """Return minimum distance in front 60-degree arc."""
        if not scan.ranges:
            return float("inf")
        n = len(scan.ranges)
        # Front arc: -30deg to +30deg (indices wrap around 0)
        arc = n // 6  # 60deg out of 360deg
        front_ranges = list(scan.ranges[:arc]) + list(scan.ranges[-arc:])
        valid = [r for r in front_ranges if not math.isnan(r) and r > 0.01]
        return min(valid) if valid else float("inf")

    def _apply_speed(
        self, mode: OperatingMode, scan: LaserScan | None = None
    ) -> None:
        """Publish velocity command scaled by operating mode."""
        cmd = TwistStamped()
        cmd.header.stamp = self.get_clock().now().to_msg()
        cmd.header.frame_id = "base_link"

        speed = self.CRUISE_SPEED * mode.speed_scale

        # Obstacle avoidance: turn if something is close ahead
        if scan is not None and speed > 0:
            front_dist = self._min_front_distance(scan)
            if front_dist < self.SAFE_DISTANCE:
                # Stop forward, turn in place
                speed = 0.0
                cmd.twist.angular.z = self.TURN_SPEED
            else:
                cmd.twist.angular.z = 0.0
        elif mode != OperatingMode.FULL_AUTO:
            cmd.twist.angular.z = 0.0

        cmd.twist.linear.x = speed
        self._vel_pub.publish(cmd)


def main() -> None:
    rclpy.init()
    node = SafeTurtleBot()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        node.get_logger().info("[VERONICA] Shutting down safety node")
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
