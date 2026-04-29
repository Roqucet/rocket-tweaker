# Rocket Tweaker

A tool for converting The Talos Principle: Reawakened `.level` & `.episode` files used in custom campaigns to and from JSON for easier editing

`.level` & `.episode` files are largely handled by Unreal Engine, with each script/object having a `Serialize` function. This results in a file format similar to unreal games saves (GVAS). It's possible those tools can read/edit .episode & .level files. However, this discord message from Croteam staff implies otherwise:
> Puzzle Editor author did do a lot of customization as to how the custom levels are serialized, not sure if there is a name for the resulting format.\
> https://discord.com/channels/464411560563965953/1315739667202834484/1359821476093624383

## Usage
```
$ python3 rocket-tweaker.py -h
usage: rocket-tweaker.py [-h] [-o OUTPUT] [-e] [--use_lengths] input_file

A tool for converting The Talos Principle: Reawakened `.level` & `.episode` files used in custom campaigns to and from JSON for easier editing. Lets you dump a file to .json for manual editing, or create a .level/.episode from .json. Will save a backup when trying to overwrite a file

positional arguments:
  input_file            /path/to/input. File extension determins conversion type - `.episode/.level` -> `.json` | `.json` -> `.level`

optional arguments:
  -h, --help            show this help message and exit
  -o OUTPUT, --output OUTPUT
                        /path/to/output
  -e, --episode         If set, will use the `.episode` extenstion for output file
  --use_lengths         If set, will use the data lengths found in the JSON. Otherwise, will ignore data lengths and calculate them dynamically
```
