"""Gera o icone do Monitor CPU Cooler (PNG + ICO multiple sizes)."""
import math
from PIL import Image, ImageDraw, ImageFilter

S = 1024  # supersampling
OUT = r"E:\LLM\MyMonitorCPUCooler"


def P(x, y):
    """Escala coordenadas de um canvas 256x256 para o canvas de 1024."""
    return (int(x * S / 256), int(y * S / 256))


def main():
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Fundo: quadrado arredondado escuro com leve degradê
    for y in range(S):
        t = y / S
        c = (int(13 + 10 * t), int(19 + 14 * t), int(28 + 20 * t), 255)
        d.line([(0, y), (S, y)], fill=c)
    mask = Image.new("L", (S, S), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.22), fill=255)
    img.putalpha(mask)

    cx, cy = 104, 130
    R = 74
    cx_s, cy_s = P(cx, cy)
    R_s = int(R * S / 256)

    # Camada de brilho (glow) do ventilador
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse([cx_s - R_s - 18, cy_s - R_s - 18, cx_s + R_s + 18, cy_s + R_s + 18],
               outline=(0, 190, 255, 130), width=int(S * 0.035))
    glow = glow.filter(ImageFilter.GaussianBlur(int(S * 0.022)))
    img.alpha_composite(glow)
    d = ImageDraw.Draw(img)

    # Pás do ventilador (6 pás curvadas)
    for angle in range(0, 360, 60):
        blade = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        bd = ImageDraw.Draw(blade)
        pts = [
            P(cx, cy),
            P(cx + 24, cy - 8),
            P(cx + 62, cy - 40),
            P(cx + 74, cy - 8),
            P(cx + 50, cy + 26),
            P(cx + 14, cy + 20),
        ]
        bd.polygon(pts, fill=(0, 196, 255, 235))
        bd.polygon(pts, outline=(120, 240, 255, 255))
        blade = blade.rotate(angle, center=(cx_s, cy_s), resample=Image.Resampling.BICUBIC)
        img.alpha_composite(blade)
        d = ImageDraw.Draw(img)

    # Aro externo do ventilador
    d.ellipse([cx_s - R_s, cy_s - R_s, cx_s + R_s, cy_s + R_s],
              outline=(0, 212, 255, 255), width=int(S * 0.022))
    d.ellipse([cx_s - R_s + 14, cy_s - R_s + 14, cx_s + R_s - 14, cy_s + R_s - 14],
              outline=(0, 150, 200, 180), width=int(S * 0.008))

    # Cubo central
    hub = int(23 * S / 256)
    d.ellipse([cx_s - hub, cy_s - hub, cx_s + hub, cy_s + hub],
              fill=(12, 24, 34, 255), outline=(0, 220, 255, 255), width=int(S * 0.012))
    d.ellipse([cx_s - 7, cy_s - 7, cx_s + 7, cy_s + 7], fill=(0, 220, 255, 255))

    # Termômetro
    tx, ty = 192, 78
    tw = int(17 * S / 256)
    top_y = P(tx, ty)[1]
    bot_y = P(tx, 178)[1]
    tx_s = P(tx, ty)[0]
    d.rounded_rectangle([tx_s - tw // 2, top_y, tx_s + tw // 2, bot_y],
                        radius=tw // 2, fill=(230, 245, 255, 255),
                        outline=(0, 180, 230, 255), width=int(S * 0.006))
    bulb_r = int(22 * S / 256)
    bulb_y = P(tx, 190)[1]
    d.ellipse([tx_s - bulb_r, bulb_y - bulb_r, tx_s + bulb_r, bulb_y + bulb_r],
              fill=(255, 96, 72, 255), outline=(255, 190, 160, 255), width=int(S * 0.006))
    # Coluna de mercúrio
    merc_y = P(tx, 108)[1]
    d.rounded_rectangle([tx_s - tw // 2 + 8, merc_y, tx_s + tw // 2 - 8, bulb_y],
                        radius=tw // 2, fill=(255, 96, 72, 255))
    # Marcas de escala
    for i in range(4):
        y = P(tx, 96 + i * 20)[1]
        d.line([(tx_s + tw // 2 + 6, y), (tx_s + tw // 2 + 16, y)],
               fill=(180, 230, 255, 255), width=int(S * 0.005))

    img = img.resize((256, 256), Image.Resampling.LANCZOS)
    png = OUT + r"\cooler_icon.png"
    ico = OUT + r"\cooler_icon.ico"
    img.save(png)
    img.save(ico, sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                         (64, 64), (128, 128), (256, 256)])
    print("gerado:", png)
    print("gerado:", ico)


if __name__ == "__main__":
    main()
