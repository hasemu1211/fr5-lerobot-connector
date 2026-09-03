from pathlib import Path
import subprocess, tempfile, unittest
import numpy as np
from tools.data_factory.curator.review.render import render_review_mp4

class RenderTest(unittest.TestCase):
    def test_real_h264_three_panel_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            output=Path(temporary)/"review.mp4"; mask=np.ones((48,64),dtype=np.uint8)
            frames=[(np.full((48,64,3),i*40,dtype=np.uint8),None,np.full((48,64,3),255-i*40,dtype=np.uint8)) for i in range(4)]
            render_review_mp4(frames,output,keep_mask=mask,width=64,height=48,fps=10)
            codec=subprocess.run(["ffprobe","-v","error","-select_streams","v:0","-show_entries","stream=codec_name,width,height","-of","csv=p=0",output],check=True,capture_output=True,text=True).stdout.strip()
            self.assertEqual(codec,"h264,192,48")

if __name__ == "__main__": unittest.main()
