import pytest
from pathlib import Path
from coderover.agents import reflect , ReflectorResult
from coderover.verifier.verification import VerifierError

class TestReflector:
    def test_single_error(self):
        """测试：单个错误能否生成修复方案"""
        # 准备一个模拟的 VerifierError
        error = VerifierError(
            tool="pytest",
            file="tests/test_math.py",
            line=15,
            error_type="AssertionError",
            message="assert 1 + 1 == 3",
            context="test_add"
        )
        
        #reflector = Reflector()
        result = reflect([error], Path("."))
        
        # 断言：返回的是 ReflectorResult
        assert isinstance(result, ReflectorResult)
        # 断言：有根因分析
        assert len(result.root_cause) > 0
        # 断言：有修复方案
        assert len(result.fix_plans) > 0
        # 断言：第一个修复方案的 file 不为空
        assert result.fix_plans[0].file != ""
    
    def test_error_classification(self):
        """测试：错误分类是否准确"""
        #reflector = Reflector()
        from coderover.agents.reflector import _classify_error
     

        # 语法错误
        error = VerifierError(tool="pytest", file="test.py", line=1, 
                              error_type="SyntaxError", message="invalid syntax", context="")
        assert _classify_error(error.error_type, error.tool) == "syntax"
        
        # 类型错误
        error = VerifierError(tool="mypy", file="test.py", line=1, 
                              error_type="override", message="Signature incompatible", context="")
        assert _classify_error(error.error_type, error.tool) == "type"
        
        # 规范错误
        error = VerifierError(tool="ruff", file="test.py", line=1, 
                              error_type="F401", message="import unused", context="")
        assert _classify_error(error.error_type, error.tool) == "style"
        
        # 逻辑错误
        error = VerifierError(tool="pytest", file="test.py", line=1, 
                              error_type="AssertionError", message="assert False", context="")
        assert _classify_error(error.error_type, error.tool) == "logic"
    
    def test_context_extraction(self):
        """测试：能否正确提取代码上下文"""
        # 创建一个临时测试文件
        import tempfile
        from coderover.agents.reflector import _get_code_context

        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
            f.write("def add(a, b):\n")
            f.write("    return a + b\n")
            f.write("\n")
            f.write("def test_add():\n")
            f.write("    assert add(1, 1) == 3\n")  # 第5行
            temp_path = f.name
        
       # reflector = Reflector()
        context = _get_code_context(Path(temp_path), line=5, context_lines=2)
        
        # 应该包含第3-7行（前后各2行）
        assert "def test_add():" in context
        assert "assert add(1, 1) == 3" in context
        
        # 清理
        Path(temp_path).unlink()