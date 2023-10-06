
def test_assumed_noop():
    """
    We are assuming the NOP code is 9.
    Double check that it is.
    """
    import opcode
    NOP_VALUE: int = opcode.opmap['NOP']
    assert NOP_VALUE == 9
