import unittest
import AI_Music_Processor as ui

class ComponentPackageWizardTests(unittest.TestCase):
    def test_two_import_actions_are_visible_and_connected_at_wizard_size(self):
        root=ui.tk.Tk();root.geometry('760x560');calls=[]
        frame=ui.build_first_run_wizard(root,{}, {},on_import=lambda:calls.append('directory'),on_import_package=lambda:calls.append('archive'))
        frame.pack(fill='both',expand=True);root.update()
        try:
            for button in (frame.import_button,frame.import_package_button,frame.finish_button):
                self.assertTrue(button.winfo_ismapped())
                self.assertGreater(button.winfo_height(),1)
                self.assertLessEqual(button.winfo_rootx()+button.winfo_width(),root.winfo_rootx()+root.winfo_width())
                self.assertLessEqual(button.winfo_rooty()+button.winfo_height(),root.winfo_rooty()+root.winfo_height())
            frame.import_button.invoke();frame.import_package_button.invoke()
            self.assertEqual(calls,['directory','archive'])
        finally:
            ui.dispose_tk_variables(frame);root.destroy()

if __name__=='__main__':unittest.main()
