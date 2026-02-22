"""
Base plugin interface for DevHealth extensions
"""

from abc import ABC, abstractmethod
from typing import List
from ..src.devhealth import HealthCheck

class DevHealthPlugin(ABC):
    """Base class for DevHealth plugins"""
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Plugin name"""
        pass
    
    @property  
    @abstractmethod
    def description(self) -> str:
        """Plugin description"""
        pass
    
    @abstractmethod
    def run_checks(self) -> List[HealthCheck]:
        """Run health checks and return results"""
        pass
    
    def is_available(self) -> bool:
        """Check if this plugin should run (e.g., tool is installed)"""
        return True
