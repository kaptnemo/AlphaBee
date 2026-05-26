from simplegeneric import generic

@generic
def func_generic(item):
    return "generic func"

@func_generic.when_type(int)
def func_generic_int(item):
    return f"int func: {item}"

@func_generic.when_type(str)
def func_generic_str(item):
    return f"str func: {item}"


from enum import StrEnum

class Color(StrEnum):
    RED = "red"
    GREEN = "green"
    BLUE = "blue"


@func_generic.when_object(Color.RED)
def func_generic_red(item):
    return f"red func: {item.value}"


@func_generic.when_object(Color.BLUE)
def func_generic_blue(item):
    return f"blue func: {item.value}"


def test_func_generic():
    assert func_generic(123) == "int func: 123"
    assert func_generic("hello") == "str func: hello"
    assert func_generic(Color.RED) == "red func: red"
    assert func_generic(Color.GREEN) == "str func: green"
    assert func_generic(Color.BLUE) == "blue func: blue"

    print("All tests passed!")

if __name__ == "__main__":
    test_func_generic()