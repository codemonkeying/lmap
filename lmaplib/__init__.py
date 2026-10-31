"""lmap's code, one file per concern, in load order (NN_name.py). bin/lmap executes every file into its own module
namespace, so all names stay module globals of `lmap`: tests can patch L.STATE / L.install_file and every caller sees
it, and the embedded scripts are unchanged byte for byte (they feed build signatures). Moving names into real modules
with their own imports happens one concern at a time; tests/t_golden.py checks nothing changed."""
