"""caseDict template settings shared by generated drafts and native reports."""
import json


# The keys mirror caseMeta's public discipline and analysis values.
_RESULT_TITLES = {
    'general': '后处理结果',
    'fluid': '流场与流体性能结果',
    'solid': '变形、应力与结构响应',
    'thermal': '温度场与热传递结果',
    'electromagnetic': '电磁场与响应结果',
    'acoustic': '声场与频谱结果',
    'multiphysics': '耦合场与界面传递结果',
    'custom': '自定义物理结果',
}
_ANALYSIS_TOPICS = {
    'steady': '稳态分析关注收敛后的场量、积分量和守恒量。',
    'transient': '瞬态分析关注采样时刻、响应历程与峰值出现时间。',
    'modal': '模态分析关注各阶固有频率、振型及所用归一化。',
    'harmonic': '谐响应分析关注激励频率、响应幅相和频带峰值。',
    'static': '静力分析关注载荷与约束下的平衡量和场量极值。',
    'dynamic': '动力分析关注载荷历程、响应峰值和采样时间间隔。',
    'optimization': '优化分析关注目标函数、约束和可核验的候选结果。',
    'parametric': '参数分析关注工况参数、输出变化和可比较的运行条件。',
    'UQ': '不确定度分析关注输入来源、传播方法和结果的扩展不确定度。',
}


def profile(parsed):
    settings = parsed.get('report') or {}
    if not isinstance(settings, dict):
        raise ValueError('report 必须是字典块')
    selected = settings.get('template', 'auto')
    if type(selected) is not str or (selected != 'auto' and selected not in _RESULT_TITLES):
        raise ValueError('report.template 必须是 auto 或已支持的学科模板')
    meta = parsed.get('caseMeta')
    if not isinstance(meta, dict):
        meta = {}
    if selected == 'auto':
        discipline = meta.get('discipline')
        selected = discipline if isinstance(discipline, str) and discipline in _RESULT_TITLES else 'general'
    analysis = meta.get('analysisType')
    topic = _ANALYSIS_TOPICS.get(analysis) if isinstance(analysis, str) else None
    return {'id': selected, 'analysis': analysis if topic else None,
            'results_title': _RESULT_TITLES[selected], 'analysis_focus': topic}


def metadata(parsed):
    report=parsed.get('report') or {}
    profile(parsed)
    result={}
    reference=report.get('referenceDoc')
    if reference is not None:
        if not isinstance(reference,str):raise ValueError('report.referenceDoc 必须是 DOCX 文件路径')
        if reference.strip():result['reference-doc']=reference.strip()
    styles=report.get('css')
    if styles is not None:
        if isinstance(styles,str):styles=[styles] if styles.strip() else []
        if not isinstance(styles,list) or any(not isinstance(p,str) for p in styles):
            raise ValueError('report.css 必须是样式表路径或非空路径列表')
        styles=[p.strip() for p in styles]
        # caseDict preserves OpenFOAM list delimiters, including delimiters
        # attached to bare paths. Strip the outer pair, never path whitespace.
        if styles and styles[0].startswith('(') and styles[-1].endswith(')'):
            styles[0]=styles[0][1:]
            styles[-1]=styles[-1][:-1]
            if styles and not styles[-1]:styles.pop()
            if styles and not styles[0]:styles.pop(0)
        if any(not p for p in styles):
            raise ValueError('report.css 必须是样式表路径或非空路径列表')
        if styles:result['css']=[p.strip() for p in styles]
    return result


def frontmatter(parsed):
    values=metadata(parsed)
    if not values:return ''
    # JSON strings/lists are YAML values too; paths cannot inject YAML keys.
    return '---\n'+'\n'.join(k+': '+json.dumps(v,ensure_ascii=False) for k,v in values.items())+'\n---\n\n'
