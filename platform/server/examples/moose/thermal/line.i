[Mesh]
  type = GeneratedMesh
  dim = 1
  nx = 10
[]

[Variables]
  [u]
  []
[]

[Kernels]
  [diffusion]
    type = Diffusion
    variable = u
  []
[]

[BCs]
  [left_value]
    type = DirichletBC
    variable = u
    boundary = left
    value = 0
  []
  [right_value]
    type = DirichletBC
    variable = u
    boundary = right
    value = 100
  []
[]

[Executioner]
  type = Steady
  solve_type = NEWTON
  petsc_options_iname = '-pc_type'
  petsc_options_value = 'lu'
[]

[Postprocessors]
  [midpoint]
    type = PointValue
    point = '0.5 0 0'
    variable = u
  []
[]

[Outputs]
  csv = true
  exodus = true
[]
