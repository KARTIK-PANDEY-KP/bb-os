#!/usr/bin/env python3
"""
DevHealth - Local-First Development Environment Monitor
Automatically detects and suggests fixes for broken development tools.

Inspired by the "fix your tools" philosophy - don't work around broken tools, fix them first!
"""

import os
import sys
import json
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime

@dataclass
class HealthCheck:
    name: str
    status: str  # "healthy", "warning", "error"
    message: str
    fix_suggestion: Optional[str] = None
    performance_info: Optional[Dict] = None

class DevHealthMonitor:
    def __init__(self):
        self.checks = []
        self.start_time = time.time()
    
    def run_command(self, cmd: List[str], timeout: int = 10) -> Tuple[int, str, str]:
        """Run a command and return exit code, stdout, stderr"""
        try:
            result = subprocess.run(
                cmd, 
                capture_output=True, 
                text=True, 
                timeout=timeout
            )
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return -1, "", f"Command timed out after {timeout}s"
        except FileNotFoundError:
            return -1, "", f"Command not found: {cmd[0]}"
    
    def check_python_environment(self) -> HealthCheck:
        """Check Python installation and common issues"""
        # Check Python version
        code, stdout, stderr = self.run_command([sys.executable, "--version"])
        if code != 0:
            return HealthCheck(
                "Python", "error", 
                f"Python not working: {stderr}",
                "Reinstall Python or check PATH"
            )
        
        python_version = stdout.strip()
        
        # Check pip
        code, pip_out, pip_err = self.run_command([sys.executable, "-m", "pip", "--version"])
        pip_status = "available" if code == 0 else "missing"
        
        # Check for virtual environment
        venv_status = "active" if hasattr(sys, 'real_prefix') or sys.base_prefix != sys.prefix else "none"
        
        message = f"{python_version}, pip: {pip_status}, venv: {venv_status}"
        
        if pip_status == "missing":
            return HealthCheck(
                "Python", "warning", message,
                "Install pip: curl https://bootstrap.pypa.io/get-pip.py | python"
            )
        
        return HealthCheck("Python", "healthy", message)
    
    def check_git_environment(self) -> HealthCheck:
        """Check Git installation and configuration"""
        code, stdout, stderr = self.run_command(["git", "--version"])
        if code != 0:
            return HealthCheck(
                "Git", "error",
                "Git not installed",
                "Install git: apk add git (Alpine) or apt install git (Ubuntu)"
            )
        
        git_version = stdout.strip()
        
        # Check git config
        code, name_out, _ = self.run_command(["git", "config", "user.name"])
        code2, email_out, _ = self.run_command(["git", "config", "user.email"])
        
        config_status = "configured" if code == 0 and code2 == 0 else "incomplete"
        message = f"{git_version}, config: {config_status}"
        
        if config_status == "incomplete":
            return HealthCheck(
                "Git", "warning", message,
                "Configure git: git config --global user.name 'Your Name' && git config --global user.email 'you@example.com'"
            )
        
        return HealthCheck("Git", "healthy", message)
    
    def check_docker_environment(self) -> HealthCheck:
        """Check Docker installation and daemon status"""
        code, stdout, stderr = self.run_command(["docker", "--version"])
        if code != 0:
            return HealthCheck(
                "Docker", "error",
                "Docker not installed",
                "Install Docker from https://docker.com"
            )
        
        docker_version = stdout.strip()
        
        # Check if Docker daemon is running
        start_check = time.time()
        code, info_out, info_err = self.run_command(["docker", "info"], timeout=5)
        docker_time = time.time() - start_check
        
        if code != 0:
            return HealthCheck(
                "Docker", "warning",
                f"{docker_version}, daemon: not running",
                "Start Docker daemon: systemctl start docker or start Docker Desktop"
            )
        
        perf_info = {"response_time": round(docker_time, 2)}
        status = "healthy" if docker_time < 2 else "warning"
        message = f"{docker_version}, daemon: running ({docker_time:.1f}s response)"
        
        fix_suggestion = None
        if docker_time > 2:
            fix_suggestion = "Docker daemon slow - check system resources or restart Docker"
        
        return HealthCheck("Docker", status, message, fix_suggestion, perf_info)
    
    def check_node_environment(self) -> HealthCheck:
        """Check Node.js installation"""
        code, stdout, stderr = self.run_command(["node", "--version"])
        if code != 0:
            return HealthCheck(
                "Node.js", "warning",
                "Node.js not installed",
                "Install Node.js from https://nodejs.org or use package manager"
            )
        
        node_version = stdout.strip()
        
        # Check npm
        code, npm_out, _ = self.run_command(["npm", "--version"])
        npm_status = npm_out.strip() if code == 0 else "missing"
        
        message = f"{node_version}, npm: {npm_status}"
        return HealthCheck("Node.js", "healthy", message)
    
    def check_disk_space(self) -> HealthCheck:
        """Check available disk space"""
        try:
            import shutil
            total, used, free = shutil.disk_usage('/')
            free_gb = free / (1024**3)
            used_percent = (used / total) * 100
            
            if free_gb < 1:
                return HealthCheck(
                    "Disk Space", "error",
                    f"{free_gb:.1f}GB free ({used_percent:.1f}% used)",
                    "Clean up disk space: remove unused files, docker images, etc."
                )
            elif free_gb < 5:
                return HealthCheck(
                    "Disk Space", "warning",
                    f"{free_gb:.1f}GB free ({used_percent:.1f}% used)",
                    "Consider cleaning up disk space soon"
                )
            
            return HealthCheck(
                "Disk Space", "healthy",
                f"{free_gb:.1f}GB free ({used_percent:.1f}% used)"
            )
        except Exception as e:
            return HealthCheck("Disk Space", "error", f"Could not check: {e}")
    
    def run_all_checks(self) -> List[HealthCheck]:
        """Run all environment health checks"""
        print("🔍 Scanning development environment...")
        
        checks = [
            self.check_python_environment(),
            self.check_git_environment(), 
            self.check_docker_environment(),
            self.check_node_environment(),
            self.check_disk_space()
        ]
        
        return checks
    
    def format_report(self, checks: List[HealthCheck]) -> str:
        """Format health check results as a report"""
        total_time = time.time() - self.start_time
        
        report = []
        report.append("=" * 60)
        report.append("🏥 DEVHEALTH REPORT")
        report.append("=" * 60)
        report.append(f"Scan completed in {total_time:.2f}s at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        report.append("")
        
        healthy_count = sum(1 for c in checks if c.status == "healthy")
        warning_count = sum(1 for c in checks if c.status == "warning")
        error_count = sum(1 for c in checks if c.status == "error")
        
        report.append(f"📊 SUMMARY: {healthy_count} healthy, {warning_count} warnings, {error_count} errors")
        report.append("")
        
        for check in checks:
            status_icon = {"healthy": "✅", "warning": "⚠️", "error": "❌"}[check.status]
            report.append(f"{status_icon} {check.name}: {check.message}")
            
            if check.fix_suggestion:
                report.append(f"   💡 Fix: {check.fix_suggestion}")
            
            if check.performance_info:
                perf_str = ", ".join(f"{k}={v}" for k, v in check.performance_info.items())
                report.append(f"   ⚡ Performance: {perf_str}")
            
            report.append("")
        
        if error_count > 0 or warning_count > 0:
            report.append("🔧 RECOMMENDATION: Fix the issues above to improve your development workflow!")
        else:
            report.append("🎉 Your development environment looks healthy!")
        
        return "\n".join(report)

def main():
    if len(sys.argv) > 1 and sys.argv[1] == "scan":
        monitor = DevHealthMonitor()
        checks = monitor.run_all_checks()
        report = monitor.format_report(checks)
        print(report)
        
        # Exit with appropriate code
        has_errors = any(c.status == "error" for c in checks)
        sys.exit(1 if has_errors else 0)
    else:
        print("DevHealth - Local-First Development Environment Monitor")
        print("Usage: python devhealth.py scan")
        print("\nEmbodying the 'fix your tools' philosophy!")

if __name__ == "__main__":
    main()
