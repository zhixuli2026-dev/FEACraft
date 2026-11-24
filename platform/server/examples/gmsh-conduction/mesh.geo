// Unit cube with 64 hexahedra and physical patches for OpenFOAM conduction.
SetFactory("OpenCASCADE");
Box(1) = {0, 0, 0, 1, 1, 1};
eps = 1e-6;
xmin[] = Surface In BoundingBox {-eps, -eps, -eps, eps, 1+eps, 1+eps};
xmax[] = Surface In BoundingBox {1-eps, -eps, -eps, 1+eps, 1+eps, 1+eps};
rest[] = Surface{:};
rest[] -= xmin[];
rest[] -= xmax[];
Physical Surface("xMin") = {xmin[]};
Physical Surface("xMax") = {xmax[]};
Physical Surface("insulated") = {rest[]};
Physical Volume("solid") = {1};
Transfinite Curve{:} = 5;
Transfinite Surface{:};
Recombine Surface{:};
Transfinite Volume{:};
Mesh.MshFileVersion = 2.2;
Mesh.Binary = 0;
