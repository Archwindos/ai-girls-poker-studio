# 可随工具分发的渲染资源

`NotoSansSC-Regular.ttf` 与 `NotoSansSC-Bold.ttf` 从项目既有的 Google Noto Sans SC 可变字体分别固定 wght=400、700，使用 fontTools 4.67.0 `instantiateVariableFont` 生成。许可证原文在 `OFL-NotoSansSC.txt`。运行时直接使用静态字体，不需要 fontTools，也不依赖 Windows 系统字体。新字体可能使字形和字宽与旧稿不同，新导出必须作为新批次复核。

`PSOcoated_v3.icc` 为既有商家稿使用的 PSO Coated v3 配置，复制后保留原字节。它仍是尚未取得印厂指定 ICC 前的暂定转换配置，不能代替印厂打样确认。
