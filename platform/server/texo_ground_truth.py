"""Render the FormulaNet benchmark corpus with WSL pdfLaTeX and Poppler."""
import json
from pathlib import Path
import subprocess
import sys

from PIL import Image, ImageChops


FORMULAS = {
    'energy': r'E=mc^{2}',
    'linear': r'x+1=3',
    'subscripts': r'a_{i}+a_{j}=a_{k}',
    'greek': r'\alpha+\beta=\gamma',
    'fraction': r'\frac{a+b}{c-d}',
    'root': r'\sqrt{x^{2}+y^{2}}',
    'series': r'\sum_{i=1}^{n}x_{i}',
    'calculus': r'\int_{0}^{1}x^{2}\,dx=\frac{1}{3}',
    'limit': r'\lim_{x\to0}\frac{\sin x}{x}=1',
    'gradient': r'\nabla\cdot\mathbf{u}=0',
    'partial': r'\frac{\partial T}{\partial t}=\alpha\nabla^{2}T',
    'matrix': r'\begin{pmatrix}a&b\\c&d\end{pmatrix}',
    'piecewise': r'f(x)=\begin{cases}x&x\geq0\\-x&x<0\end{cases}',
    'fraction_nested': r'\frac{1}{1+\frac{1}{x}}',
}


def build(folder, distro):
    folder = folder.resolve()
    folder.mkdir(parents=True, exist_ok=True)
    wsl = ['wsl.exe', '-d', distro, '--exec']

    def native(path):
        return subprocess.check_output(wsl + ['wslpath', '-a', str(path)], text=True).strip()

    tex = folder / 'ground-truth.tex'
    tex.write_text(r'\documentclass{article}\usepackage{amsmath,amssymb}\pagestyle{empty}\begin{document}' + '\n'
        + '\n\\newpage\n'.join(r'\[\displaystyle ' + value + r'\]' for value in FORMULAS.values())
        + '\n' + r'\end{document}', encoding='ascii')
    subprocess.run(wsl + ['pdflatex', '-halt-on-error', '-interaction=batchmode', '-no-shell-escape',
                         '-output-directory=' + native(folder), native(tex)], check=True, capture_output=True)
    subprocess.run(wsl + ['pdftoppm', '-r', '200', '-png', native(folder / 'ground-truth.pdf'),
                         native(folder / 'native-render')], check=True)
    rendered = sorted(folder.glob('native-render-*.png'))
    assert len(rendered) == len(FORMULAS)
    truth = []
    for (name, latex), render in zip(FORMULAS.items(), rendered):
        with Image.open(render) as full:
            gray = full.convert('L')
            bounds = ImageChops.invert(gray).point(lambda pixel: 255 if pixel > 18 else 0).getbbox()
            assert bounds
            left, top, right, bottom = bounds
            image = gray.crop((left - 14, top - 14, right + 14, bottom + 14))
            path = folder / (name + '.png')
            image.save(path)
            truth.append({'name': name, 'latex': latex, 'png': path.name,
                          'width': image.width, 'height': image.height})
    (folder / 'truth.json').write_text(json.dumps(truth, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    build(Path(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else 'vibeflow-linux')
