# Homebrew formula for the OmniVec CLI.
#
# Source of truth for the Homebrew tap at:
#   https://github.com/AzureCosmosDB/homebrew-omnivec
#
# This copy lives in the main repo so it ships with the source and can be
# diffed alongside CLI changes. After every release, copy this file to
# `Formula/omnivec.rb` in the tap repo.
class Omnivec < Formula
  desc "Manage OmniVec ingestion pipelines, sources, destinations and embedding models"
  homepage "https://github.com/AzureCosmosDB/OmniVec"
  version "1.1.3"
  license "MIT"

  on_macos do
    on_arm do
      url "https://github.com/AzureCosmosDB/OmniVec/releases/download/v#{version}/omnivec-v#{version}-darwin-arm64"
      sha256 "af3edb5f4173c5726304d3dfb2fa8491ac801b3f87f29e57eb4dbbd38e0a69ed"
    end
    on_intel do
      url "https://github.com/AzureCosmosDB/OmniVec/releases/download/v#{version}/omnivec-v#{version}-darwin-amd64"
      sha256 "b7a45c3536103424e5401de9a406d5c7184632579557dfe06ec68058db96ad14"
    end
  end

  on_linux do
    on_arm do
      url "https://github.com/AzureCosmosDB/OmniVec/releases/download/v#{version}/omnivec-v#{version}-linux-arm64"
      sha256 "d502edefafdb76c35c3858384f48e95ed7bc7e7c2248020e1e4b90ab366dc178"
    end
    on_intel do
      url "https://github.com/AzureCosmosDB/OmniVec/releases/download/v#{version}/omnivec-v#{version}-linux-amd64"
      sha256 "fe60b096207eee58e36d5e6bbe5e72109f32e3a9fd41656e9617175570e2c249"
    end
  end

  def install
    arch = Hardware::CPU.arm? ? "arm64" : "amd64"
    os   = OS.mac? ? "darwin" : "linux"
    bin.install "omnivec-v#{version}-#{os}-#{arch}" => "omnivec"
  end

  test do
    assert_match "omnivec", shell_output("#{bin}/omnivec --help 2>&1", 0).downcase
  end
end
