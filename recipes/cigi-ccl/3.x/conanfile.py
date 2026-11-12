from conan import ConanFile
from conan.tools.cmake import CMake, CMakeToolchain, cmake_layout
from conan.tools.files import copy, get, replace_in_file, rmdir
import os

required_conan_version = ">=2.0"


class CigiCclConan(ConanFile):
    """CIGI Class Library 3.x (CIGI 3.3). Based on ConanCenter's cigi-ccl/4.0.6a recipe.

    What differs from the 4.x recipe, because the 3.3 build files differ:
      - CMakeLists.txt has no cmake_minimum_required at all (4.x had it in the wrong place)
      - there are no "Header Files\\" source groups to patch
      - libraries are called cigicl / cigicl_static (+ "_debug"), not ccl_dll / ccl_lib (+ "D")
      - headers are installed to include/cigicl
    """
    name = "cigi-ccl"
    description = "Industry standard communication with compliant image generators"
    license = "LGPL-2.1-or-later"
    homepage = "https://cigi.sourceforge.io/product_ccl.php"
    topics = ("simulation", "interface engines", "data visualization")
    package_type = "library"
    settings = "os", "arch", "compiler", "build_type"
    options = {
        "shared": [True, False],
        "fPIC": [True, False],
    }
    default_options = {
        "shared": False,
        "fPIC": True,
    }

    def config_options(self):
        if self.settings.os == "Windows":
            del self.options.fPIC

    def configure(self):
        if self.options.shared:
            self.options.rm_safe("fPIC")

    def layout(self):
        cmake_layout(self, src_folder="src")

    def source(self):
        # URL and sha256 come from conandata.yml (the archive lives in our Nexus raw repo)
        get(self, **self.conan_data["sources"][self.version], strip_root=True)

    def generate(self):
        tc = CMakeToolchain(self)
        tc.generate()

    @property
    def _target(self):
        return "cigicl-shared" if self.options.shared else "cigicl-static"

    def _patch_sources(self):
        cmakelists = os.path.join(self.source_folder, "CMakeLists.txt")
        # No cmake_minimum_required in this old file. Modern CMake needs one, and 3.15 is what
        # lets Conan choose the MSVC runtime (/MD, /MT) from the profile.
        replace_in_file(self, cmakelists, "PROJECT(ccl)",
                        "CMAKE_MINIMUM_REQUIRED(VERSION 3.15)\nPROJECT(ccl)")
        # It installs both the static and the shared target; we only build one of them.
        replace_in_file(self, cmakelists, "INSTALL(TARGETS cigicl-static cigicl-shared",
                        f"INSTALL(TARGETS {self._target}")

    def build(self):
        self._patch_sources()
        cmake = CMake(self)
        cmake.configure()
        cmake.build(target=self._target)

    def package(self):
        cmake = CMake(self)
        cmake.install()
        copy(self, "license.html", self.source_folder, os.path.join(self.package_folder, "licenses"))
        copy(self, "COPYING", self.source_folder, os.path.join(self.package_folder, "licenses"))
        rmdir(self, os.path.join(self.package_folder, "share"))
        rmdir(self, os.path.join(self.package_folder, "lib", "pkgconfig"))

    def package_info(self):
        self.cpp_info.set_property("pkg_config_name", "cigicl")
        # Upstream's own pkg-config file uses include/cigicl, i.e. #include "CigiHostSession.h".
        # "include" is listed too so #include <cigicl/CigiHostSession.h> also works.
        self.cpp_info.includedirs = [os.path.join("include", "cigicl"), "include"]

        windows = self.settings.os == "Windows"
        # CMakeLists.txt: CMAKE_DEBUG_POSTFIX "_debug" for MSVC / MinGW only
        suffix = "_debug" if windows and self.settings.build_type == "Debug" else ""
        if self.options.shared:
            self.cpp_info.libs = ["cigicl" + suffix]
            if windows:
                self.cpp_info.defines.append("CCL_DLL")   # selects __declspec(dllimport)
        else:
            # static is "cigicl_static" on Windows so it can't clash with the DLL's import lib
            self.cpp_info.libs = [("cigicl_static" if windows else "cigicl") + suffix]

        if windows:
            # CigiTypes.h picks its Windows type definitions with "#if defined(WIN32)" (not
            # _WIN32). The library is built with WIN32 defined, so consumers must match.
            self.cpp_info.defines.append("WIN32")
        if self.settings.os in ["Linux", "FreeBSD"]:
            self.cpp_info.system_libs.append("m")
