"""Compare two scan signatures without treating similar layouts as the same page.

A repeat of the same spread is small in both the average pixel change and the
share of blocks that changed. A new page with a similar text layout still
changes many local blocks, so it is kept.
"""


def is_same_page(mean_diff, changed_fraction, mean_limit, fraction_limit):
    """Return True only when both difference measures fall below their limits."""
    return mean_diff < mean_limit and changed_fraction < fraction_limit


def image_difference(left, right, block_rows, block_cols, block_threshold):
    """Return mean absolute difference and the fraction of changed blocks.

    ``left`` and ``right`` are grayscale images addressed as ``image[y][x]``
    with values from 0 to 255. Differences are normalized to 0–1.
    """
    height = len(left)
    width = len(left[0]) if height else 0
    if height == 0 or width == 0 or len(right) != height or len(right[0]) != width:
        raise ValueError("用于比较的两幅图像尺寸不一致或为空")
    if block_rows < 1 or block_cols < 1:
        raise ValueError("分块数量必须为正")
    block_h = height // block_rows
    block_w = width // block_cols
    if block_h < 1 or block_w < 1:
        raise ValueError("检测图小于分块网格，请增大 detect_width 或减少分块")

    total = 0
    changed = 0
    for block_y in range(block_rows):
        for block_x in range(block_cols):
            block_sum = 0
            y0 = block_y * block_h
            x0 = block_x * block_w
            for y in range(y0, y0 + block_h):
                left_row = left[y]
                right_row = right[y]
                for x in range(x0, x0 + block_w):
                    block_sum += abs(int(left_row[x]) - int(right_row[x]))
            total += block_sum
            if (block_sum / (block_h * block_w)) / 255.0 >= block_threshold:
                changed += 1
    pixels = block_rows * block_cols * block_h * block_w
    mean_diff = (total / pixels) / 255.0
    return mean_diff, changed / (block_rows * block_cols)
