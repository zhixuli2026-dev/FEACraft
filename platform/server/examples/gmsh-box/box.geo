// Three-dimensional OpenCASCADE box with physical groups for gmshToFoam.
SetFactory("OpenCASCADE");
Box(1) = {0, 0, 0, 1, 1, 1};
Physical Surface("walls") = {1, 2, 3, 4, 5, 6};
Physical Volume("fluid") = {1};
Mesh.CharacteristicLengthMin = 0.25;
Mesh.CharacteristicLengthMax = 0.25;
Mesh.MshFileVersion = 2.2;
Mesh.Binary = 0;
