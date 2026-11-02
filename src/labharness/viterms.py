"""The terminals of LabVIEW's functions by their number on the function (parmIndex), named, for each function Kelvin
names (vigraph.PRIMS): which input of Subtract is x, which of Select's values is used when the selector is true.

The names are lvkit's (github.com/pragmatest-dev/lvkit, src/lvkit/data/primitives, Apache License 2.0), a clean-room
table of LabVIEW's functions made from NI's public documentation and LabVIEW's own files; each agrees with what
Kelvin's reader already relied on (x and y of the arithmetic, Append True/False String's, VISA Write's).
"""
from typing import Dict

TERMINALS: Dict[int, Dict[int, str]] = {
    1050: {0: 'sum', 1: 'y', 2: 'x'},   # Add
    1051: {0: 'difference', 1: 'y', 2: 'x'},   # Subtract
    1052: {0: 'product', 1: 'y', 2: 'x'},   # Multiply
    1053: {0: 'quotient', 1: 'y', 2: 'x'},   # Divide
    1054: {0: 'abs_x', 1: 'x'},   # Absolute Value
    1055: {0: 'nearest integer value', 1: 'number'},   # Round To Nearest
    1056: {0: 'floor(x/y)', 1: 'x-y*floor(x/y)', 2: 'y', 3: 'x'},   # Quotient & Remainder
    1057: {0: 'x+1', 1: 'x'},   # Increment
    1058: {0: 'x-1', 1: 'x'},   # Decrement
    1061: {0: 'result', 1: 'y', 2: 'x'},   # And
    1062: {0: 'result', 1: 'y', 2: 'x'},   # Or
    1064: {0: 'not_x', 1: 'x'},   # Not
    1076: {0: 'ceiling', 1: 'x'},   # Round Toward +Infinity
    1077: {0: 'string', 1: 'path'},   # Path To String
    1078: {0: 'path', 1: 'string'},   # String To Path
    1102: {0: 'equal', 1: 'y', 2: 'x'},   # Equal?
    1103: {0: 'greater_or_equal', 1: 'y', 2: 'x'},   # Greater Or Equal?
    1104: {0: 'x_less_or_equal_y', 1: 'y', 2: 'x'},   # Less Or Equal?
    1105: {0: 'result', 1: 'y', 2: 'x'},   # Not Equal?
    1108: {0: 'min(x,y)', 1: 'max(x,y)', 2: 'y', 3: 'x'},   # Max & Min
    1110: {0: 'result', 1: 'y', 2: 'x'},   # Greater?
    1111: {0: 'x_less_than_y', 1: 'y', 2: 'x'},   # Less?
    1112: {0: 'is_empty', 1: 'string_or_path'},   # Empty String/Path?
    1113: {0: 'x_equals_zero', 1: 'x'},   # Equal To 0?
    1114: {0: 'result', 1: 'x'},   # Greater Or Equal To 0?
    1115: {0: 'result', 1: 'x'},   # Less Or Equal To 0?
    1116: {0: 'x != 0?', 1: 'x'},   # Not Equal To 0?
    1128: {0: 'not_a_refnum', 1: 'value'},   # Not A Number/Path/Refnum?
    1142: {0: 'long_integer', 1: 'number'},   # To Long Integer
    1144: {0: 'uint16_value', 1: 'number'},   # To Unsigned Word Integer
    1145: {0: 'unsigned 32bit integer', 1: 'number'},   # To Unsigned Long Integer
    1147: {0: 'result', 1: 'number'},   # To Double Precision Float
    1155: {0: '64bit integer', 1: 'number'},   # To Quad Integer
    1166: {0: 'x', 1: 'type', 2: 'x_cast'},   # Type Cast
    1169: {0: 'array', 1: 'cluster'},   # Cluster To Array
    1170: {0: 'input', 1: 'high', 2: 'low'},   # Split Number
    1171: {0: '(hi.lo)', 1: 'lo', 2: 'hi'},   # Join Numbers
    1180: {0: 'decimal_integer_string', 1: 'width', 2: 'number'},   # Number To Decimal String
    1183: {0: 'Engineering string', 1: 'use system decimal point (T)', 2: 'precision (6)', 3: 'width (-)', 4: 'number'},   # Number To Engineering String
    1184: {0: 'number', 1: 'offset_past_number', 2: 'default', 3: 'offset', 4: 'string'},   # Decimal String To Number
    1187: {0: 'number', 1: 'offset past number', 2: 'use system decimal point (T)', 3: 'default (0 dbl)', 4: 'offset', 5: 'string'},   # Fract/Exp String To Number
    1207: {0: 'arccos(x)', 1: 'x'},   # Inverse Cosine
    1300: {0: 'millisecond_timer_value'},   # Tick Count
    1302: {0: 'millisecond_timer_value', 1: 'milliseconds_to_wait'},   # Wait (ms)
    1303: {0: 'current_time'},   # Get Date/Time In Seconds
    1340: {0: 'true', 1: 'button_name', 2: 'message'},   # One Button Dialog
    1341: {0: 't_button_pressed', 1: 'f_button_name', 2: 't_button_name', 3: 'message'},   # Two Button Dialog
    1419: {0: 'appended path', 1: 'name or relative path', 2: 'base path'},   # Build Path
    1502: {0: 'length', 1: 'string'},   # String Length
    1503: {0: 'substring', 1: 'length (rest)', 2: 'offset (0)', 3: 'string'},   # String Subset
    1506: {0: 'F-format string', 1: 'use system decimal point (T)', 2: 'precision (6)', 3: 'width (-)', 4: 'number'},   # Number To Fractional String
    1510: {0: 'output_string', 1: 'line_index', 2: 'multi_line_string', 3: 'string'},   # Pick Line
    1511: {0: 'output string', 1: 'selector', 2: 'false string', 3: 'true string', 4: 'string ("")'},   # Append True/False String
    1512: {0: 'output string', 1: 'value (0)', 2: 'format string', 3: 'string ("")'},   # Format Value
    1516: {0: 'result', 1: 'f_value', 2: 'selector', 3: 't_value'},   # Select
    1535: {0: 'string', 1: 'regular expression', 2: 'offset', 3: 'before substring', 4: 'match substring', 5: 'after substring', 6: 'offset past match'},   # Match Pattern
    1539: {0: 'array', 1: 'delimiter', 2: 'array_type', 3: 'spreadsheet_string', 4: 'format_string'},   # Spreadsheet String To Array
    1540: {0: 'spreadsheet_string', 1: 'delimiter', 2: 'array', 3: 'format_string'},   # Array To Spreadsheet String
    1600: {0: 'time string', 1: 'date string', 2: 'want seconds? (F)', 3: 'time stamp', 4: 'date format (0)'},   # Get Date/Time String
    1609: {0: 'string', 1: 'unsigned byte array'},   # Byte Array To String
    1713: {0: 'error out', 3: 'connection ID out', 8: 'error in (no error)', 10: 'abort (F)', 11: 'connection ID'},   # TCP Close
    1714: {0: 'error out', 2: 'bytes written', 3: 'connection ID out', 8: 'error in (no error)', 9: 'timeout ms (25000)', 10: 'data in', 11: 'connection ID'},   # TCP Write
    1715: {0: 'error out', 3: 'connection ID', 6: 'local port', 8: 'error in (no error)', 9: 'timeout ms (60000)', 10: 'remote port or service name', 11: 'address'},   # TCP Open
    1716: {0: 'error out', 2: 'data out', 3: 'connection ID out', 7: 'mode (standard)', 8: 'error in (no error)', 9: 'timeout ms (25000)', 10: 'bytes to read', 11: 'connection ID'},   # TCP Read
    1809: {0: 'size', 1: 'array'},   # Array Size
    1900: {0: 'reversed_array', 1: 'array'},   # Reverse 1D Array
    1902: {0: 'transposed_array', 1: 'array'},   # Transpose 2D Array
    1903: {0: 'sum', 1: 'array'},   # Add Array Elements
    1922: {0: 'error out', 8: 'error in (no error)', 11: 'VISA resource name'},   # VISA Close
    1923: {0: 'error out', 2: 'return count', 3: 'find list', 8: 'error in (no error)', 10: 'search mode (0)', 11: 'expression ("")'},   # VISA Find Resource
    1925: {0: 'error out', 1: 'return count', 2: 'read buffer', 3: 'VISA resource name out', 8: 'error in (no error)', 10: 'byte count', 11: 'VISA resource name'},   # VISA Read
    1926: {0: 'error out', 2: 'return count', 3: 'VISA resource name out', 8: 'error in (no error)', 10: 'write buffer', 11: 'VISA resource name'},   # VISA Write
    1927: {0: 'error out', 1: 'VISA resource name out', 2: 'error in (no error)', 3: 'VISA resource name'},   # VISA Clear
    1928: {0: 'error out', 1: 'status byte', 2: 'VISA resource name out', 3: 'error in (no error)', 4: 'VISA resource name'},   # VISA Read STB
    2076: {0: 'event_reg_refnum', 3: 'error_in', 7: 'error_out'},   # Unregister For Events
    2301: {0: 'error out', 1: 'VISA resource name out', 2: 'error in (no error)', 3: 'size (4096)', 4: 'mask (16)', 5: 'VISA resource name'},   # VISA Set I/O Buffer Size
    2302: {0: 'error out', 3: 'VISA resource name out', 8: 'error in (no error)', 10: 'mask (16)', 11: 'VISA resource name'},   # VISA Flush I/O Buffer
    2308: {0: 'error out', 3: 'VISA resource name', 7: 'timeout (0)', 8: 'error in (no error)', 9: 'access mode', 10: 'duplicate session (F)', 11: 'VISA resource name'},   # VISA Open
    3913: {0: 'replaced substring', 1: 'result string', 2: 'length', 3: 'offset', 4: 'substring', 5: 'string'},   # Replace Substring
    8003: {0: 'error in', 3: 'error out', 8: 'variant', 9: 'type', 11: 'data'},   # Variant To Data
    8011: {0: 'error_out', 8: 'error_in', 11: 'reference'},   # Close Reference
    8016: {0: 'specific class reference', 1: 'error out', 2: 'target class', 3: 'reference', 4: 'error in'},   # To More Specific Class
    8017: {0: 'reference', 1: 'target class', 2: 'generic class reference'},   # To More Generic Class
    8050: {0: 'error_out', 1: 'cancelled', 3: 'refnum', 6: 'advisory_locking', 7: 'prompt', 8: 'error_in', 9: 'access', 10: 'operation', 11: 'file_path'},   # Open/Create/Replace File
    8051: {0: 'error_out', 1: 'eof', 2: 'data', 3: 'refnum_out', 5: 'byte_order', 7: 'type', 8: 'error_in', 9: 'datalog_type', 10: 'count', 11: 'refnum'},   # Read from Binary File
    8052: {0: 'error_out', 3: 'path', 4: 'error_in', 7: 'refnum'},   # Close File
    8055: {0: 'error out', 1: 'cancelled', 3: 'created path', 7: 'prompt', 8: 'error in', 11: 'path (use dialog)'},   # Create Folder
    8056: {0: 'error_out', 1: 'cancelled', 3: 'path_out', 7: 'prompt', 8: 'error_in', 9: 'entire_hierarchy', 10: 'confirm', 11: 'path'},   # Delete
    8070: {0: 'error_out', 1: 'cancelled', 2: 'text', 3: 'refnum_out', 7: 'prompt', 8: 'error_in', 10: 'count', 11: 'file'},   # Read from Text File
    8073: {0: 'error_out', 3: 'refnum_out', 4: 'error_in', 5: 'from', 6: 'offset', 7: 'refnum'},   # Set File Position
    8080: {0: 'error out', 1: 'cancelled', 3: 'refnum out', 7: 'prompt', 8: 'error in', 10: 'text', 11: 'file (use dialog)'},   # Write to Text File
    8201: {0: 'variant', 1: 'anything'},   # To Variant
    9108: {0: 'name', 1: 'element_data_type', 2: 'create_if_not_found', 3: 'error_in', 4: 'max_queue_size', 8: 'queue_out', 10: 'created', 11: 'error_out'},   # Obtain Queue
    9111: {0: 'queue', 1: 'element', 2: 'timeout_ms', 3: 'error_in', 8: 'queue_out', 10: 'timed_out', 11: 'error_out'},   # Enqueue Element
}
