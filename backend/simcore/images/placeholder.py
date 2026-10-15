from __future__ import annotations

import html


def placeholder_svg(title: str, kind: str, subtitle: str = "", size: tuple[int, int] = (640, 400)) -> str:
    width, height = size
    safe_title = html.escape(title)
    safe_kind = html.escape(kind)
    safe_subtitle = html.escape(subtitle)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
<defs><linearGradient id="wash" x2="1" y2="1"><stop stop-color="#d4c39a"/><stop offset="1" stop-color="#718071"/></linearGradient></defs>
<rect width="100%" height="100%" fill="url(#wash)"/>
<path d="M0 {height * .76} Q{width * .25} {height * .48} {width * .48} {height * .78} T{width} {height * .67} V{height} H0Z" fill="#253b37" opacity=".78"/>
<rect x="7%" y="7%" width="86%" height="86%" rx="18" fill="none" stroke="#f5e8c8" stroke-width="3" opacity=".8"/>
<text x="50%" y="44%" text-anchor="middle" fill="#fff5df" font-family="serif" font-size="{max(18, width // 22)}">{safe_title}</text>
<text x="50%" y="55%" text-anchor="middle" fill="#fff5df" font-family="sans-serif" font-size="{max(12, width // 42)}">{safe_kind}</text>
<text x="50%" y="91%" text-anchor="middle" fill="#fff5df" font-family="sans-serif" font-size="{max(10, width // 56)}">{safe_subtitle}</text>
</svg>"""
