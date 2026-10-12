fn main() {
    // CI sets FABRIC_RELEASE_COMMIT for builds of main. The app compares it with
    // the latest release to offer updates; local builds leave it unset and skip that.
    println!("cargo:rerun-if-env-changed=FABRIC_RELEASE_COMMIT");
    let commit = std::env::var("FABRIC_RELEASE_COMMIT").unwrap_or_default();
    println!("cargo:rustc-env=FABRIC_BUILD_COMMIT={commit}");
    tauri_build::build()
}
