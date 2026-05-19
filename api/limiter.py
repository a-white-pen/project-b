"""
Shared rate limiter instance for all api/ routes.

Functions:
  (none — exports the module-level limiter object for use in route files and app.py)
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
