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
  [transient]
    type = TimeDerivative
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
  type = Transient
  solve_type = NEWTON
  petsc_options_iname = '-pc_type'
  petsc_options_value = 'lu'
  dt = 0.1
  end_time = 0.4
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
  checkpoint = true
[]
