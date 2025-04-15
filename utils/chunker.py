def group_paragraphs_to_chunks(elements, max_words=2000):
    chunks, buffer, word_count = [], [], 0
    for elem in elements:
        if elem["type"] == "TEXT":
            words = len(elem["content"].split())
            if word_count + words > max_words and buffer:
                chunks.append({"type": "TEXT", "content": "\n".join(buffer)})
                buffer, word_count = [], 0
            buffer.append(elem["content"])
            word_count += words
        elif elem["type"] == "FIGURE":
            if buffer:
                chunks.append({"type": "TEXT", "content": "\n".join(buffer)})
                buffer, word_count = [], 0
            chunks.append(elem)
    if buffer:
        chunks.append({"type": "TEXT", "content": "\n".join(buffer)})
    return chunks
