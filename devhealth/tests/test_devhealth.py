#!/usr/bin/env python3
"""
Tests for DevHealth tool
"""

import unittest
import sys
import os

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from devhealth import DevHealthMonitor, HealthCheck

class TestDevHealth(unittest.TestCase):
    
    def setUp(self):
        self.monitor = DevHealthMonitor()
    
    def test_python_check(self):
        """Test Python environment checking"""
        check = self.monitor.check_python_environment()
        self.assertIsInstance(check, HealthCheck)
        self.assertEqual(check.name, "Python")
        self.assertIn(check.status, ["healthy", "warning", "error"])
    
    def test_git_check(self):
        """Test Git environment checking"""  
        check = self.monitor.check_git_environment()
        self.assertIsInstance(check, HealthCheck)
        self.assertEqual(check.name, "Git")
        self.assertIn(check.status, ["healthy", "warning", "error"])
    
    def test_disk_check(self):
        """Test disk space checking"""
        check = self.monitor.check_disk_space()
        self.assertIsInstance(check, HealthCheck)
        self.assertEqual(check.name, "Disk Space")
        self.assertIn(check.status, ["healthy", "warning", "error"])
    
    def test_run_command(self):
        """Test command execution helper"""
        code, stdout, stderr = self.monitor.run_command(["echo", "test"])
        self.assertEqual(code, 0)
        self.assertEqual(stdout.strip(), "test")
    
    def test_run_command_timeout(self):
        """Test command timeout handling"""
        code, stdout, stderr = self.monitor.run_command(["sleep", "20"], timeout=1)
        self.assertEqual(code, -1)
        self.assertIn("timed out", stderr.lower())
    
    def test_all_checks(self):
        """Test running all checks"""
        checks = self.monitor.run_all_checks()
        self.assertIsInstance(checks, list)
        self.assertGreater(len(checks), 0)
        
        for check in checks:
            self.assertIsInstance(check, HealthCheck)
            self.assertIn(check.status, ["healthy", "warning", "error"])
    
    def test_report_formatting(self):
        """Test report formatting"""
        sample_checks = [
            HealthCheck("Test Tool", "healthy", "All good"),
            HealthCheck("Broken Tool", "error", "Not working", "Fix it"),
        ]
        
        report = self.monitor.format_report(sample_checks)
        self.assertIn("DEVHEALTH REPORT", report)
        self.assertIn("Test Tool", report)
        self.assertIn("Broken Tool", report)
        self.assertIn("Fix it", report)

if __name__ == "__main__":
    unittest.main()
