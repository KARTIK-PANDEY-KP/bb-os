"""
VS Code plugin for DevHealth - checks VS Code installation and common issues
"""

import os
import json
import subprocess
from pathlib import Path
from typing import List
from .base import DevHealthPlugin, HealthCheck

class VSCodePlugin(DevHealthPlugin):
    @property
    def name(self) -> str:
        return "VS Code"
    
    @property
    def description(self) -> str:
        return "Checks VS Code installation, extensions, and configuration"
    
    def run_command(self, cmd: List[str], timeout: int = 5):
        """Helper to run commands"""
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return result.returncode, result.stdout, result.stderr
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return -1, "", "Command failed"
    
    def is_available(self) -> bool:
        """Check if VS Code is installed"""
        code, _, _ = self.run_command(["code", "--version"])
        return code == 0
    
    def run_checks(self) -> List[HealthCheck]:
        checks = []
        
        # Check VS Code installation
        code, stdout, stderr = self.run_command(["code", "--version"])
        if code != 0:
            checks.append(HealthCheck(
                "VS Code", "warning",
                "VS Code not installed or not in PATH",
                "Install VS Code and ensure 'code' command is available"
            ))
            return checks
        
        version_lines = stdout.strip().split('\n')
        version = version_lines[0] if version_lines else "unknown"
        
        # Check for common settings file
        settings_path = Path.home() / ".vscode" / "settings.json" 
        settings_status = "found" if settings_path.exists() else "default"
        
        # Check installed extensions
        code, ext_out, _ = self.run_command(["code", "--list-extensions"])
        extension_count = len(ext_out.strip().split('\n')) if ext_out.strip() else 0
        
        message = f"Version {version}, settings: {settings_status}, extensions: {extension_count}"
        
        # Performance check - see if VS Code starts quickly
        # (This would be too intrusive for a real scan, but good for demo)
        
        checks.append(HealthCheck("VS Code", "healthy", message))
        
        # Check for common problematic extensions or configurations
        if extension_count > 50:
            checks.append(HealthCheck(
                "VS Code Extensions", "warning",
                f"Many extensions installed ({extension_count})",
                "Consider disabling unused extensions for better performance"
            ))
        
        return checks
