from django.test import TestCase
from . import tasks
from .models import *


# Create your tests here.
class TestEvesde(TestCase):
    
    def test_update_contacts(self):    
        tasks.update_alliance_contacts()