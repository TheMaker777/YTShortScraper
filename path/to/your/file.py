    # ffmpeg concat command with fflags to regenerate timestamps
    command = f'ffmpeg -y -fflags +genpts -f concat -safe 0 -i {input_file} -c copy {output_filename}'
    os.system(command)