"""A foreign recipe. Stands in for an R or Julia script; the contract is identical.

One JSON job file is the only argument. Inputs arrive as files whose paths the job
names, and the result goes to the output path it names. stdout and stderr are logging
only, so a stray print here cannot corrupt a stored value.

The .npy reader below is deliberately hand-rolled: the point of a foreign recipe is
that the host does not dictate the language or its libraries, and numpy need not be
installed for this to read a matrix matview produced.
"""

import ast
import json
import struct
import sys


def read_npy(path):
    """Read a 2-D little-endian float64 .npy file without numpy."""
    with open(path, "rb") as handle:
        raw = handle.read()
    if raw[:6] != b"\x93NUMPY":
        raise ValueError(f"{path} is not a .npy file")
    major = raw[6]
    if major == 1:
        header_len = struct.unpack("<H", raw[8:10])[0]
        start = 10
    else:
        header_len = struct.unpack("<I", raw[8:12])[0]
        start = 12
    header = ast.literal_eval(raw[start : start + header_len].decode("ascii"))
    if header["descr"] != "<f8" or header["fortran_order"]:
        raise ValueError(f"expected C-ordered little-endian float64, got {header}")
    rows, cols = header["shape"]
    body = raw[start + header_len :]
    values = struct.unpack(f"<{rows * cols}d", body[: rows * cols * 8])
    return [list(values[i * cols : (i + 1) * cols]) for i in range(rows)]


def main():
    with open(sys.argv[1], encoding="utf-8") as handle:
        job = json.load(handle)
    params = job["params"]
    print(f"fitting {params['model']} for {params['dataset']}", file=sys.stderr)

    matrix = read_npy(job["inputs"]["matrix"])
    size = len(matrix)

    # A real implementation would fit a tree here. This one reports something derived
    # from the matrix, so that a change upstream is visible downstream.
    result = {
        "model": params["model"],
        "n_taxa": size,
        "total_distance": round(sum(sum(row) for row in matrix), 6),
        "newick": "(" + ",".join(f"t{i}" for i in range(size)) + ");",
    }

    with open(job["output"], "w", encoding="utf-8") as handle:
        json.dump(result, handle)


if __name__ == "__main__":
    main()
