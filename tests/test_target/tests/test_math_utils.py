import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.math_utils import add, subtract, multiply, divide  



def test_add():  
    assert add(2, 3) == 5  
  
def test_subtract():  
    assert subtract(5, 3) == 2  
  
def test_multiply():  
    assert multiply(2, 3) == 6  
  
def test_divide():  
    assert divide(10, 2) == 5  
    assert divide(10, 0) == "Error: Division by zero" 
